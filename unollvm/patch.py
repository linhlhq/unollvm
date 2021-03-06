import logging

import angr
import capstone
import claripy

log = logging.getLogger('unollvm')

def _make_capstone_reg_to_name():
    names = filter(lambda x: x.startswith('X86_REG_'), dir(capstone.x86_const))
    result = {}
    for name in names:
        enum = getattr(capstone.x86_const, name)
        result[enum] = name[8:].lower()
    return result
capstone_reg_to_name = _make_capstone_reg_to_name()

def sym_is_val(sym):
    return sym.op == 'BVV'

def sym_val(sym):
    assert sym_is_val(sym)
    return sym.args[0]

def get_insn_operand(state, operand):
    if operand.type == capstone.x86_const.X86_OP_REG:
        reg_name = capstone_reg_to_name[operand.reg]
        mask = (1 << 64) - 1
        if reg_name[-1] == 'd':
            reg_name = reg_name[:-1]
            mask = (1 << 32) - 1
        sym = getattr(state.regs, reg_name)
        if sym_is_val(sym):
            return sym_val(sym) & mask
        else:
            return None
    else:
        raise Exception('Cannot handle operand type {}'.format(operand.type))

class Patch(object):

    def __init__(self, proj, shape, control, ks):
        self.proj = proj
        self.shape = shape
        self.control = control
        self.ks = ks
        self.disas_cache = dict()
        self.patches = dict()

        self.analyze()

    def asm(self, addr, text):
        code, _ = self.ks.asm(text, addr=addr)
        return code

    def make_patch(self, addr, code):
        for n in range(len(code)):
            self.patches[addr + n] = [code[n],]

    def disas(self, addr):
        if addr not in self.disas_cache:
            block = self.proj.factory.block(addr)
            insns = block.capstone.insns
            for angr_insn in insns:
                cs_insn = angr_insn.insn
                self.disas_cache[cs_insn.address] = cs_insn
        return self.disas_cache[addr]

    def get_swvar(self, state):
        # Assume state variable is 4-byte integer type.
        return state.memory.load(self.control.swvar_addr, 4).reversed

    def exec_insn(self, state):
        try:
            succ = state.step(num_inst=1)
        except angr.errors.SimZeroDivisionException:
            log.warn('Skipping instruction {} due to divide-by-zero'.format(state.regs.pc))
            return state
        assert len(succ.successors) == 1
        return succ[0]

    def exec_insns(self, state, insn_addrs, on_insn):
        for addr in insn_addrs:
            if on_insn:
                if not on_insn(state, addr):
                    break
            state.regs.pc = addr
            state = self.exec_insn(state)
        return state

    def exec_block(self, state, addr, on_insn=None):
        '''
        Execute a basic block, but ignore any call instruction,
        because we want to keep the analysis inside function.

        on_insn: function (state, addr) -> bool
            the execution stops whtn on_insn returns a falsey.

        returns: (state, addr)
        '''
        block = self.proj.factory.block(addr)
        jk = block.vex.jumpkind
        insn_addrs = block.instruction_addrs
        if jk == 'Ijk_Boring':
            # Assume we do not meet conditional branch.
            # We will only execute the prologue blocks and switch-case bodies,
            # which do not contain conditional branch instructions in a
            # control-flow-flattening obfuscated program.
            state = self.exec_insns(state, insn_addrs, on_insn)
            return state, sym_val(state.regs.pc)
        elif jk == 'Ijk_Call':
            # Do not execute call instruction.
            # Instead put placeholder return value to eax register.
            state = self.exec_insns(state, insn_addrs[:-1], on_insn)
            state.regs.eax = claripy.BVS('retval_from_{:x}'.format(insn_addrs[-1]), 32)
            return state, addr + block.size
        else:
            raise Exception('Cannot handle jumpkind {}'.format(jk))

    def patch_dispatcher(self):
        # Directly jump to the initial switch case block
        target = self.control.swmap[self.init_swval]
        insn_addr = self.shape.dispatcher
        text = 'jmp 0x{:x}'.format(target)
        code = self.asm(insn_addr, text)
        # Check if there is enough room for the patch
        assert len(code) <= self.proj.factory.block(self.shape.dispatcher).size
        self.make_patch(insn_addr, code)

    def analyze_dispatcher(self):
        '''
        Execute prologue blocks until initial switch value is determined.
        '''
        addr = self.shape.func.addr
        state = self.proj.factory.blank_state(addr=addr)
        # Assume that the function prologue starts with "push bp; mov bp, sp"
        state.regs.sp = self.control.default_bp() + self.proj.arch.bytes

        # Run until we know initial concrete value of the switch variable.
        self.init_swval = None
        def check_swvar(state, addr):
            var = self.get_swvar(state)
            if var.op == 'BVV':
                self.init_swval = sym_val(var)
                return False
            return True

        while addr != self.shape.collector and (addr not in self.shape.exits):
            state, addr = self.exec_block(state, addr, check_swvar)
            if self.init_swval is not None:
                log.info('  Initial switch variable is {:x} ({:x})'.format(
                    self.init_swval, self.control.swmap[self.init_swval]))
                self.patch_dispatcher()
                return

        if self.init_swval is None:
            raise Exception('Cannot find initial switch value')

    def patch_uncond(self, block_addr, target):
        block = self.proj.factory.block(block_addr)
        insn_addr = block.instruction_addrs[-1]

        text = 'jmp 0x{:x}'.format(target)
        code = self.asm(insn_addr, text)

        last_insn = self.disas(insn_addr)
        # Check if the last instruction of the block is jump to collector.
        assert last_insn.mnemonic == 'jmp'
        # Check if there is enough romm for the patch.
        assert last_insn.size >= len(code)
        self.make_patch(insn_addr, code)

    def patch_cond(self, addr, target):
        cmov_insn = self.disas(addr)
        assert cmov_insn.mnemonic[:4] == 'cmov'
        cc = cmov_insn.mnemonic[4:]

        # Conditional jump instead of conditional move.
        text = 'j{} 0x{:x}'.format(cc, target)
        code = self.asm(addr, text)

        # Check if there is enough room for the patch.
        block = self.proj.factory.block(addr)
        last_insn_addr = block.instruction_addrs[-1]
        cavity_size = last_insn_addr - addr
        assert len(code) <= cavity_size

        # Fill the remaining bytes by nops.
        # Assume last instructions of the conditial jump block looks like:
        #   cmovcc __, __
        #   mov [swvar_addr], __
        #   jmp collector
        code += [0x90]*(cavity_size- len(code))
        self.make_patch(addr, code)

    def analyze_case(self, case):
        '''
        Execute each switch-case block to recover control transfer.
        '''
        state = self.proj.factory.blank_state(addr=case)
        state.regs.bp = self.control.default_bp()

        orig_swvar = self.get_swvar(state)
        self.cmov_info = []
        def record_cmov(state, addr):
            # Remember switch variable changes using cmovcc insturction.
            insn = self.disas(addr)
            if insn.mnemonic.startswith('cmov'):
                f = get_insn_operand(state, insn.operands[0])
                t = get_insn_operand(state, insn.operands[1])
                # If two operands of cmovcc instruction belong to the
                # switch values, then we assume that this cmovcc determines
                # the next switch variable.
                if f in self.control.swmap and t in self.control.swmap:
                    self.cmov_info.append((addr, f, t))
            return True

        addr = case
        while True:
            state, next_addr = self.exec_block(state, addr, record_cmov)
            if next_addr == self.shape.collector:
                break
            addr = next_addr

        # If we sense that the switch variable is changed,
        curr_swvar = self.get_swvar(state)
        if not (orig_swvar == curr_swvar).is_true():
            if len(self.cmov_info) == 1:
                cmov_addr, f, t = self.cmov_info[0]
                f_block = self.control.swmap[f]
                t_block = self.control.swmap[t]
                log.info('    Cond   {:x} -> {:x} or {:x}'.format(case, f_block, t_block))
                # Jump to true case when cmovcc condition is true.
                self.patch_cond(cmov_addr, t_block)
                # Jump to false case otherwise.
                self.patch_uncond(addr, f_block)
            elif sym_is_val(curr_swvar):
                target = self.control.swmap[sym_val(curr_swvar)]
                log.info('    Uncond {:x} -> {:x}'.format(case, target))
                self.patch_uncond(addr, target)
            else:
                raise Exception('Cannot determine control transfer for case block {:x}'.format(case))

    def mute_block(self, addr):
        block = self.proj.factory.block(addr)
        code = [0x90]*block.size
        self.make_patch(block.addr, code)

    def mute_dispatcher(self):
        for comparator in self.control.cmps:
            self.mute_block(comparator)
        self.mute_block(self.shape.collector)

    def analyze(self):
        self.mute_dispatcher()
        self.analyze_dispatcher()
        for case in self.control.swmap.itervalues():
            if case not in self.shape.exits:
                self.analyze_case(case)
        return False

    def __repr__(self):
        return "Patch({}, {}, {})".format(self.proj, self.shape, self.control)

    def __str__(self):
        return self.__repr__()
