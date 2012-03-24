'''
Trap keyboard interrupts.  No rights reserved; use at your own risk.

@author: Stacy Prowell (http://stacyprowell.com)
@url: http://stacyprowell.com/blog/2009/03/30/trapping-ctrlc-in-python/
'''
import signal

class BreakHandler:
    '''
    Trap CTRL-C, set a flag, and keep going.  This is very useful for
    gracefully exiting database loops while simulating transactions.

    To use this, make an instance and then enable it.  You can check
    whether a break was trapped using the trapped property.

    # Create and enable a break handler.
    ih = BreakHandler()
    ih.enable()
    for x in big_set:
        complex_operation_1()
        complex_operation_2()
        complex_operation_3()
        # Check whether there was a break.
        if ih.trapped:
            # Stop the loop.
            break
    ih.disable()
    # Back to usual operation...
    '''

    def __init__(self, emphatic=9):
        '''
        Create a new break handler.

        @param emphatic: This is the number of times that the user must
                    press break to *disable* the handler.  If you press
                    break this number of times, the handler is automagically
                    disabled, and one more break will trigger an old
                    style keyboard interrupt.  The default is nine.  This
                    is a Good Idea, since if you happen to lose your
                    connection to the handler you can *still* disable it.
        '''
        self._count = 0
        self._enabled = False
        self._emphatic = emphatic
        self._oldhandler = None
        return

    def _reset(self):
        '''
        Reset the trapped status and count.  You should not need to use this
        directly; instead you can disable the handler and then re-enable it.
        This is better, in case someone presses CTRL-C during this operation.
        '''
        self._count = 0
        return

    def enable(self):
        '''
        Enable trapping of the break.  This action also resets the
        handler count and trapped properties.
        '''
        if not self._enabled:
            self._reset()
            self._enabled = True
            self._oldhandler = signal.signal(signal.SIGINT, self)
        return

    def disable(self):
        '''
        Disable trapping the break.  You can check whether a break
        was trapped using the count and trapped properties.
        '''
        if self._enabled:
            self._enabled = False
            signal.signal(signal.SIGINT, self._oldhandler)
            self._oldhandler = None
        return

    def __call__(self, signame, sf):
        '''
        An break just occurred.  Save information about it and keep
        going.
        '''
        self._count += 1
        # If we've exceeded the "emphatic" count disable this handler.
        if self._count >= self._emphatic:
            self.disable()
        return

    def __del__(self):
        '''
        Python is reclaiming this object, so make sure we are disabled.
        '''
        self.disable()
        return

    @property
    def count(self):
        '''
        The number of breaks trapped.
        '''
        return self._count

    @property
    def trapped(self):
        '''
        Whether a break was trapped.
        '''
        return self._count > 0
