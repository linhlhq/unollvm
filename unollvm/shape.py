import logging

import angr

log = logging.getLogger('unollvm')

def block_contains(outer, inner):
    a = outer.addr
    b = outer.addr + outer.size
    x = inner.addr
    y = inner.addr + inner.size
    return a <= x and y <= b

class Shape(object):

    def __init__(self, proj, func):
        self.proj = proj
        self.func = func
        self.prolog_cache = None
        self.out_degree = self.func.graph.out_degree()

        self.is_ollvm = False
        self.collector = None
        self.dispatcher = None
        self.exits = []

        self.is_ollvm = self.analyze()

    def non_call_bbl(self, addr):
        '''
        The first basic block that follows `addr` with jumpkind Ijk_Boring.
        '''
        block = self.proj.factory.block(addr)
        while True:
            jk = block.vex.jumpkind
            if jk == 'Ijk_Boring' or jk == 'Ijk_Ret':
                return block
            elif jk == 'Ijk_Call':
                block = self.proj.factory.block(block.addr + block.size)

    def prolog(self):
        '''
        The first basic block of the function
        Function call does not split the block.
        '''
        if not self.prolog_cache:
            self.prolog_cache = self.non_call_bbl(self.func.addr)
        return self.prolog_cache

    def is_collector(self, addr):
        ss = self.func.get_node(addr).successors()

        # Ends with an unconditional branch.
        if len(ss) != 1:
            return False
        s = ss[0]

        # Collector is a basic block, not a function.
        if not isinstance(s, angr.codenode.BlockNode):
            return False

        # Collector jumps back into the prolog.
        return block_contains(self.prolog(), s)

    def is_exit(self, addr):
        # Nodes without outgoing edges.
        block = self.non_call_bbl(addr)
        node = block.codenode
        if node in self.out_degree:
            return self.out_degree[node] == 0
        else:
            log.warn('    Assuming node {:x} is an exit node.'.format(node.addr))
            return True

    def try_consolidate_collectors(self, collectors):
        # Sometimes, a body block is directly connected to the collector
        # without a jump instruction.
        if len(collectors) != 2:
            return None
        addr0, addr1 = collectors
        node0 = self.func.get_node(addr0)
        node1 = self.func.get_node(addr1)
        if block_contains(node0, node1):
            return addr1
        elif block_contains(node1, node0):
            return addr0
        else:
            return None

    def analyze(self):
        collectors = filter(self.is_collector, self.func.block_addrs)
        if len(collectors) != 1:
            self.collector = self.try_consolidate_collectors(collectors)
            if self.collector == None:
                return False
        else:
            self.collector = collectors[0]
        log.info('  collector at {:x}'.format(self.collector))

        # Dispatcher is the jump target of the collector.
        collector_node = self.func.get_node(self.collector)
        self.dispatcher = collector_node.successors()[0].addr
        log.info('  dispatcher at {:x}'.format(self.dispatcher))

        self.exits = filter(self.is_exit, self.func.block_addrs)
        exit_list = ','.join(map(lambda n: '{:x}'.format(n), self.exits))
        log.info('  exit nodes: [{}]'.format(exit_list))
        return True

    def __repr__(self):
        return ("Shape({})".format(repr(self.func)))

    def __str__(self):
        return self.__repr__()
