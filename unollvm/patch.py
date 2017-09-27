import angr

class Patch(object):

    def __init__(self, proj, shape, control):
        self.proj = proj
        self.shape = shape
        self.control = control

    def __repr__(self):
        return "Patch({}, {}, {})".format(self.proj, self.shape, self.control)

    def __str__(self):
        return self.__repr__()