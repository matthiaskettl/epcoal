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

    REQUIRED_PATHS = ["lib", "examples", "check.py", "transformer.py", "merge.py"]

    def executable(self, tool_locator: BaseTool2.ToolLocator):
        return tool_locator.find_executable("check.py", subdir=".")

    def program_files(self, executable):
        return [executable] + self._program_files_from_executable(
            executable, self.REQUIRED_PATHS, parent_dir=False
        )

    def version(self, executable):
        # TODO: Update to true version or use the git hash
        return "1.0"

    def name(self):
        return "EPCOAL"

    def cmdline(self, executable, options, task, rlimits):
        additional_options = []
        if isinstance(task.options, dict) and task.options.get("language") == "C":
            data_model = task.options.get("data_model")
            if data_model:
                data_model_option = {"ILP32": "32", "LP64": "64"}.get(data_model)
                if data_model_option:
                    if data_model_option not in options:
                        additional_options += ["--datamodel", data_model_option]
                else:
                    raise benchexec.tools.template.UnsupportedFeatureException(
                        f"Unsupported data_model '{data_model}' defined for task '{task}'"
                    )
        return (
            [executable]
            + list(task.input_files_or_identifier)
            + options
            + additional_options
        )

    def determine_result(self, run):
        for line in reversed(run.output):
            if "Final verdict: equivalent" in line:
                return benchexec.result.RESULT_DONE + " (equivalent)"
            if "Final verdict: not equivalent" in line:
                return benchexec.result.RESULT_DONE + " (not equivalent)"
        return benchexec.result.RESULT_DONE + " (unknown)"
