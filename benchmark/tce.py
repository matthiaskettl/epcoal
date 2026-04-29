# This file is part of BenchExec, a framework for reliable benchmarking:
# https://github.com/sosy-lab/benchexec
#
# SPDX-FileCopyrightText: 2007-2020 Dirk Beyer <https://www.sosy-lab.org>
#
# SPDX-License-Identifier: Apache-2.0

import benchexec.result
from benchexec.tools.template import BaseTool2


class Tool(BaseTool2):
    """
    Tool info for invariant-learning.
    """

    REQUIRED_PATHS = ["tce.sh"]

    def executable(self, tool_locator: BaseTool2.ToolLocator):
        return tool_locator.find_executable("tce.sh", subdir=".")

    def program_files(self, executable):
        return [executable] + self._program_files_from_executable(
            executable, self.REQUIRED_PATHS, parent_dir=False
        )

    def version(self, executable):
        # TODO: Update to true version or use the git hash
        return "1.0"

    def name(self):
        return "TCE"

    def cmdline(self, executable, options, task, rlimits):
        return [executable] + options + list(task.input_files_or_identifier)

    def determine_result(self, run):
        for line in reversed(run.output):
            if "TCE equivalent" in line:
                return benchexec.result.RESULT_DONE + " (equivalent)"
            elif "TCE not equivalent" in line:
                return benchexec.result.RESULT_DONE + " (not equivalent)"
        return benchexec.result.RESULT_DONE + " (crash)"
