""" User interface functions """

import os
import sys

def termwidth():
    if 'COLUMNS' in os.environ:
        try:
            return int(os.environ['COLUMNS'])
        except ValueError:
            pass
    try:
        import termios, array, fcntl
        for dev in (sys.stdout, sys.stdin):
            try:
                fd = dev.fileno()
                if not os.isatty(fd):
                    continue
                arri = fcntl.ioctl(fd, termios.TIOCGWINSZ, '\0' * 8)
                return array.array('h', arri)[1]
            except ValueError:
                pass
    except ImportError:
        pass
    return 80


# Log levels
ERROR = 0
DEFAULT = 10
VERBOSE = 20
DEBUG = 30


# SGR foreground color codes
_colors = {
    'BLACK':    '30', 'BLACK_B':   '90',
    'RED':      '31', 'RED_B':     '91',
    'GREEN':    '32', 'GREEN_B':   '92',
    'YELLOW':   '33', 'YELLOW_B':  '93',
    'BLUE':     '34', 'BLUE_B':    '94',
    'MAGENTA':  '35', 'MAGENTA_B': '95',
    'CYAN':     '36', 'CYAN_B':    '96',
    'WHITE':    '37', 'WHITE_B':   '97' }


# Configuration
_level = DEFAULT


def status(msg, *args, **kwargs):
    """Write a status message.

    args are treated as substitutions for msg.

    The following keyword arguments are allowed:
      level    : One of DEFAULT, VERBOSE or DEBUG.
      linebreak: If True a new line is appended to msg (default: True).
      truncate : Truncate output if larger then term width (default: True).
    """
    global _level
    level = kwargs.get('level', DEFAULT)
    if level > _level:
        return
    width = termwidth()
    if args:
        msg = msg % args
    if kwargs.get('linebreak', True):
        msg = '%s%s' % (msg, os.linesep)
    if level == ERROR:
        stream = sys.stderr
    else:
        stream = sys.stdout
    if kwargs.get('truncate', True) and level != ERROR:
        add_newline = msg.endswith('\n')
        msglines = msg.splitlines()
        for no, line in enumerate(msglines):
            if len(line) > width:
                msglines[no] = line[:width-3]+"..."
        msg = os.linesep.join(msglines)
        if add_newline:
            msg = '%s%s' % (msg, os.linesep)
    if isinstance(msg, unicode):
        msg = msg.encode('utf-8')
    color = kwargs.get('color', None)
    bold =  kwargs.get('bold',  None)
    if color in _colors:
        msg = '%s%s%s' % ("\x1b["+_colors[color]+(";1" if bold else "")+"m", msg, "\x1b[0m")
    stream.write(msg)
    stream.flush()


def update_config(options):
    """Update UI configuration."""
    global _level
    _level = options.verbosity
