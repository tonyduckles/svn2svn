""" optparser helper functions """

import optparse
import textwrap

class HelpFormatter(optparse.IndentedHelpFormatter):
    """
    Modified version of certain optparse.IndentedHelpFormatter methods:
    * Respect line-breaks in parser.desription and option.help_text
    * Vertically-align long_opts
    Inspired by: http://groups.google.com/group/comp.lang.python/browse_thread/thread/6df6e6b541a15bc2/09f28e26af0699b1?pli=1
    """
    def format_description(self, description):
        if not description: return ""
        desc_width = self.width - self.current_indent
        indent = " "*self.current_indent
        bits = description.split('\n')
        formatted_bits = [
            textwrap.fill(bit,
                          desc_width,
                          initial_indent=indent,
                          subsequent_indent=indent)
            for bit in bits]
        result = "\n".join(formatted_bits) + "\n"
        return result

    def format_option_strings(self, option):
        """Return a comma-separated list of option strings & metavariables."""
        if option.takes_value():
            metavar = option.metavar or option.dest.upper()
            short_opts = [("%s" % (sopt)) if option._long_opts else \
                          (self._short_opt_fmt % (sopt, metavar))
                          for sopt in option._short_opts]
            long_opts = [self._long_opt_fmt % (lopt, metavar)
                         for lopt in option._long_opts]
        else:
            short_opts = option._short_opts
            long_opts = option._long_opts

        return ("    " if not short_opts else "")+(", ".join(short_opts + long_opts))

    def format_option(self, option):
        result = []
        opts = self.option_strings[option]
        opt_width = self.help_position - self.current_indent - 2
        if len(opts) > opt_width:
            opts = "%*s%s\n" % (self.current_indent, "", opts)
            indent_first = self.help_position
        else: # start help on same line as opts
            opts = "%*s%-*s  " % (self.current_indent, "", opt_width, opts)
            indent_first = 0
        result.append(opts)
        if option.help:
            help_text = self.expand_default(option)
            help_lines = []
            for para in help_text.split("\n"):
                help_lines.extend(textwrap.wrap(para, self.help_width))
            result.append("%*s%s\n" % (indent_first, "", help_lines[0]))
            result.extend(["%*s%s\n" % (self.help_position, "", line)
                        for line in help_lines[1:]])
        elif opts[-1] != "\n":
            result.append("\n")
        return "".join(result)

    def format_usage(self, usage):
        return usage
