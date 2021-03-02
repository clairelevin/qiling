#!/usr/bin/env python3
#
# Cross Platform and Multi Architecture Advanced Binary Emulation Framework
#

import json
from typing import Callable

from unicorn import UcError

from qiling import Qiling
from qiling.arch.x86 import GDTManager, ql_x86_register_cs, ql_x86_register_ds_ss_es, ql_x86_register_fs, ql_x86_register_gs, ql_x8664_set_gs
from qiling.const import QL_ARCH, QL_INTERCEPT
from qiling.exception import QlErrorSyscallError, QlErrorSyscallNotFound
from qiling.os.const import STDCALL, CDECL, MS64
from qiling.os.os import QlOs

from qiling.refactored.cc import QlCC, intel
from qiling.refactored.os.fcall import QlFunctionCall

from .const import Mapper
from .handle import Handle, HandleManager
from .thread import QlWindowsThread, QlWindowsThreadManagement
from .clipboard import Clipboard
from .fiber import FiberManager
from .registry import RegistryManager
from .utils import ql_x86_windows_hook_mem_error

import qiling.os.windows.dlls as api

class QlOsWindows(QlOs):
    def __init__(self, ql: Qiling):
        super(QlOsWindows, self).__init__(ql)

        self.ql = ql

        def __make_fcall_selector(atype: QL_ARCH) -> Callable[[int], QlFunctionCall]:
            __fcall_objs = {
                STDCALL: QlFunctionCall(ql, intel.stdcall(ql)),
                CDECL  : QlFunctionCall(ql, intel.cdecl(ql)),
                MS64   : QlFunctionCall(ql, intel.ms64(ql))
            }

            __selector = {
                QL_ARCH.X86  : lambda cc: __fcall_objs[cc],
                QL_ARCH.X8664: lambda cc: __fcall_objs[MS64]
            }

            return __selector[atype]

        self.fcall_selector = __make_fcall_selector(ql.archtype)
        self.fcall = None

        self.PE_RUN = True
        self.last_error = 0
        # variables used inside hooks
        self.hooks_variables = {}
        self.syscall_count = {}
        self.argv = self.ql.argv
        self.env = self.ql.env
        self.pid = self.profile.getint("KERNEL","pid")
        self.ql.hook_mem_unmapped(ql_x86_windows_hook_mem_error)
        self.automatize_input = self.profile.getboolean("MISC","automatize_input")
        self.username = self.profile["USER"]["username"]
        self.windir = self.profile["PATH"]["systemdrive"] + self.profile["PATH"]["windir"]
        self.userprofile = self.profile["PATH"]["systemdrive"] + "Users\\" + self.profile["USER"]["username"] + "\\"
        self.load()


    def load(self):
        self.setupGDT()
        # hook win api
        self.ql.hook_code(self.hook_winapi)


    def setupGDT(self):
        # setup gdt
        if self.ql.archtype == QL_ARCH.X86:
            self.gdtm = GDTManager(self.ql)
            ql_x86_register_cs(self)
            ql_x86_register_ds_ss_es(self)
            ql_x86_register_fs(self)
            ql_x86_register_gs(self)
        elif self.ql.archtype == QL_ARCH.X8664:
            ql_x8664_set_gs(self.ql)


    def setupComponents(self):
        # handle manager
        self.handle_manager = HandleManager()
        # registry manger
        self.registry_manager = RegistryManager(self.ql)
        # clipboard
        self.clipboard = Clipboard(self.ql.os)
        # fibers
        self.fiber_manager = FiberManager(self.ql)
        # thread manager
        main_thread = QlWindowsThread(self.ql)
        self.thread_manager = QlWindowsThreadManagement(self.ql, main_thread)

        # more handle manager
        new_handle = Handle(obj=main_thread)
        self.handle_manager.append(new_handle)

    # hook WinAPI in PE EMU
    def hook_winapi(self, ql: Qiling, address: int, size: int):
        if address in ql.loader.import_symbols:
            entry = ql.loader.import_symbols[address]
            api_name = entry['name']

            if api_name is None:
                api_name = Mapper[entry['dll']][entry['ordinal']]
            else:
                api_name = api_name.decode()

            api_func = self.user_defined_api[QL_INTERCEPT.CALL].get(api_name)

            if not api_func:
                api_func = getattr(api, f'hook_{api_name}')

                self.syscall_count.setdefault(api_name, 0)
                self.syscall_count[api_name] += 1

            if api_func:
                try:
                    api_func(ql, address, api_name)
                except Exception as ex:
                    ql.log.exception(ex)
                    ql.log.debug("%s Exception Found" % api_name)

                    raise QlErrorSyscallError("Windows API Implementation Error")
            else:
                ql.log.warning(f'api {api_name} is not implemented')

                if ql.debug_stop:
                    raise QlErrorSyscallNotFound("Windows API implementation not found")


    def post_report(self):
        self.ql.log.debug("Syscalls called:")
        for key, values in self.utils.syscalls.items():
            self.ql.log.debug(f'{key}:')

            for value in values:
                self.ql.log.debug(f'  {json.dumps(value):s}')

        self.ql.log.debug("Registries accessed:")
        for key, values in self.registry_manager.accessed.items():
            self.ql.log.debug(f'{key}:')

            for value in values:
                self.ql.log.debug(f'  {json.dumps(value):s}')

        self.ql.log.debug("Strings:")
        for key, values in self.utils.appeared_strings.items():
            self.ql.log.debug(f'{key}: {" ".join(str(word) for word in values)}')


    def run(self):
        if self.ql.exit_point is not None:
            self.exit_point = self.ql.exit_point

        if  self.ql.entry_point is not None:
            self.ql.loader.entry_point = self.ql.entry_point

        if self.ql.stdin != 0:
            self.stdin = self.ql.stdin

        if self.ql.stdout != 0:
            self.stdout = self.ql.stdout

        if self.ql.stderr != 0:
            self.stderr = self.ql.stderr

        try:
            if self.ql.code:
                self.ql.emu_start(self.ql.loader.entry_point, (self.ql.loader.entry_point + len(self.ql.code)), self.ql.timeout, self.ql.count)
            else:
                self.ql.emu_start(self.ql.loader.entry_point, self.exit_point, self.ql.timeout, self.ql.count)
        except UcError:
            self.emu_error()
            raise

        self.registry_manager.save()
        self.post_report()
