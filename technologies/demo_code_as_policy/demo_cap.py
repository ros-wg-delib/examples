# %% [markdown]
# Adapted from Code as Policies (https://github.com/google-research/google-research/tree/master/code_as_policies)
# Copyright 2022 Google LLC. SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations under
# the License.
#
# This version replaces the original PyBullet tabletop simulation with the PyRoboSim-based
# "delib_ws_worlds" problem setup from this repository, so that generated code calls the
# pick / place / navigate primitives exposed by `delib_ws_problem_interface`.
#
# Run `ros2 run delib_ws_worlds run --ros-args -p problem_number:=1` in a separate terminal before
# running this script, and make sure `ollama serve` is running locally with the configured model.

# %%
import ast
import re
from time import sleep

import requests
from pygments import highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import PythonLexer

import rclpy
from pyrobosim_msgs.srv import RequestWorldState
from delib_ws_problem_interface.perform_action import ACTIONS, PerformAction
from delib_ws_problem_interface.world_state import WorldState

OLLAMA_BASE_URL = "http://localhost:11434"
MODEL_NAME = "qwen2.5-coder:3b-base"


# %% [markdown]
# ## LLM backend


def ollama_generate(prompt, stop, temperature, model, max_tokens, base_url=OLLAMA_BASE_URL):
    """Calls a local Ollama server's /api/generate endpoint, mimicking the old
    openai.Completion.create() return shape."""
    while True:
        try:
            resp = requests.post(
                f"{base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                        "stop": stop,
                    },
                },
                timeout=120,
            )
            resp.raise_for_status()
            return strip_code_fences(resp.json()["response"].strip())
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            print(f"Ollama request got err {e}")
            print("Retrying after 10s. Is `ollama serve` running?")
            sleep(10)


def strip_code_fences(text):
    """Strips a leading/trailing markdown code fence (```python ... ```), which chat-tuned
    models often add."""
    match = re.match(r"^```[a-zA-Z0-9]*\n(.*?)\n?```\s*$", text, re.DOTALL)
    return match.group(1) if match else text


# %% [markdown]
# ## Code-as-Policies engine (LMP / LMPFGen)
#
# This part is generic and domain-agnostic: it is unchanged from the original Code as Policies
# implementation, aside from formatting clean-up.


class LMP:
    def __init__(self, name, cfg, lmp_fgen, fixed_vars, variable_vars):
        self._name = name
        self._cfg = cfg
        self._base_prompt = self._cfg["prompt_text"]
        self._stop_tokens = list(self._cfg["stop"])
        self._lmp_fgen = lmp_fgen
        self._fixed_vars = fixed_vars
        self._variable_vars = variable_vars
        self.exec_hist = ""

    def clear_exec_hist(self):
        self.exec_hist = ""

    def build_prompt(self, query, context=""):
        if len(self._variable_vars) > 0:
            variable_vars_imports_str = f"from utils import {', '.join(self._variable_vars.keys())}"
        else:
            variable_vars_imports_str = ""
        prompt = self._base_prompt.replace("{variable_vars_imports}", variable_vars_imports_str)

        if self._cfg["maintain_session"]:
            prompt += f"\n{self.exec_hist}"

        if context != "":
            prompt += f"\n{context}"

        use_query = f'{self._cfg["query_prefix"]}{query}{self._cfg["query_suffix"]}'
        prompt += f"\n{use_query}"

        return prompt, use_query

    def __call__(self, query, context="", **kwargs):
        prompt, use_query = self.build_prompt(query, context=context)

        code_str = ollama_generate(
            prompt=prompt,
            stop=self._stop_tokens,
            temperature=self._cfg["temperature"],
            model=self._cfg["engine"],
            max_tokens=self._cfg["max_tokens"],
        )

        if self._cfg["include_context"] and context != "":
            to_exec = f"{context}\n{code_str}"
            to_log = f"{context}\n{use_query}\n{code_str}"
        else:
            to_exec = code_str
            to_log = f"{use_query}\n{to_exec}"

        to_log_pretty = highlight(to_log, PythonLexer(), TerminalFormatter())
        print(f"LMP {self._name} exec:\n\n{to_log_pretty}\n")

        new_fs = self._lmp_fgen.create_new_fs_from_code(code_str)
        self._variable_vars.update(new_fs)

        gvars = merge_dicts([self._fixed_vars, self._variable_vars])
        lvars = kwargs

        if not self._cfg["debug_mode"]:
            exec_safe(to_exec, gvars, lvars)

        self.exec_hist += f"\n{to_exec}"

        if self._cfg["maintain_session"]:
            self._variable_vars.update(lvars)

        if self._cfg["has_return"]:
            return lvars[self._cfg["return_val_name"]]


class LMPFGen:
    def __init__(self, cfg, fixed_vars, variable_vars):
        self._cfg = cfg
        self._stop_tokens = list(self._cfg["stop"])
        self._fixed_vars = fixed_vars
        self._variable_vars = variable_vars
        self._base_prompt = self._cfg["prompt_text"]

    def create_f_from_sig(self, f_name, f_sig, other_vars=None, fix_bugs=False, return_src=False):
        print(f"Creating function: {f_sig}")

        use_query = f'{self._cfg["query_prefix"]}{f_sig}{self._cfg["query_suffix"]}'
        prompt = f"{self._base_prompt}\n{use_query}"

        f_src = ollama_generate(
            prompt=prompt,
            stop=self._stop_tokens,
            temperature=self._cfg["temperature"],
            model=self._cfg["engine"],
            max_tokens=self._cfg["max_tokens"],
        )

        if fix_bugs:
            raise NotImplementedError(
                "fix_bugs requires an edit-style endpoint, which is not supported by Ollama."
            )

        if other_vars is None:
            other_vars = {}
        gvars = merge_dicts([self._fixed_vars, self._variable_vars, other_vars])
        lvars = {}

        exec_safe(f_src, gvars, lvars)

        if f_name not in lvars:
            raise RuntimeError(
                f"Generated code did not define '{f_name}':\n{f_src}"
            )
        f = lvars[f_name]

        to_print = highlight(f"{use_query}\n{f_src}", PythonLexer(), TerminalFormatter())
        print(f"LMP FGEN created:\n\n{to_print}\n")

        if return_src:
            return f, f_src
        return f

    def create_new_fs_from_code(self, code_str, other_vars=None, fix_bugs=False, return_src=False):
        fs, f_assigns = {}, {}
        f_parser = FunctionParser(fs, f_assigns)
        f_parser.visit(ast.parse(code_str))
        for f_name, f_assign in f_assigns.items():
            if f_name in fs:
                fs[f_name] = f_assign

        if other_vars is None:
            other_vars = {}

        new_fs = {}
        srcs = {}
        for f_name, f_sig in fs.items():
            all_vars = merge_dicts([self._fixed_vars, self._variable_vars, new_fs, other_vars])
            if not var_exists(f_name, all_vars):
                f, f_src = self.create_f_from_sig(f_name, f_sig, new_fs, fix_bugs=fix_bugs, return_src=True)
                # register before recursing so self-recursive bodies aren't re-synthesized forever
                new_fs[f_name], srcs[f_name] = f, f_src

                # recursively define child_fs in the function body if needed
                f_def_body = "\n".join(ast.unparse(stmt) for stmt in ast.parse(f_src).body[0].body)
                child_vars = merge_dicts([self._fixed_vars, self._variable_vars, new_fs, other_vars])
                child_fs, child_f_srcs = self.create_new_fs_from_code(
                    f_def_body, other_vars=child_vars, fix_bugs=fix_bugs, return_src=True
                )

                if len(child_fs) > 0:
                    new_fs.update(child_fs)
                    srcs.update(child_f_srcs)

                    # redefine parent f so newly created child_fs are in scope
                    gvars = merge_dicts([self._fixed_vars, self._variable_vars, new_fs, other_vars])
                    lvars = {}

                    exec_safe(f_src, gvars, lvars)

                    f = lvars[f_name]
                    new_fs[f_name] = f

        if return_src:
            return new_fs, srcs
        return new_fs


class FunctionParser(ast.NodeTransformer):
    def __init__(self, fs, f_assigns):
        super().__init__()
        self._fs = fs
        self._f_assigns = f_assigns

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name):
            f_sig = ast.unparse(node).strip()
            f_name = ast.unparse(node.func).strip()
            self._fs[f_name] = f_sig
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            f_name = ast.unparse(node.value.func).strip()
            f_assign = ast.unparse(node).strip()
            self._f_assigns[f_name] = f_assign
        return node


def var_exists(name, all_vars):
    try:
        eval(name, all_vars)
    except NameError:
        exists = False
    else:
        exists = True
    return exists


def merge_dicts(dicts):
    return {k: v for d in dicts for k, v in d.items()}


def exec_safe(code_str, gvars=None, lvars=None):
    banned_phrases = ["import", "__"]
    for phrase in banned_phrases:
        assert phrase not in code_str, f"Banned phrase '{phrase}' found in generated code."

    if gvars is None:
        gvars = {}
    if lvars is None:
        lvars = {}
    empty_fn = lambda *args, **kwargs: None
    custom_gvars = merge_dicts([gvars, {"exec": empty_fn, "eval": empty_fn}])
    exec(code_str, custom_gvars, lvars)


# %% [markdown]
# ## PyRoboSim environment wrapper
#
# Thin wrapper around the `delib_ws_problem_interface` helpers, exposing pick / place / navigate
# as the primitives the LLM-generated code can call.


class PyRoboSimEnv:
    """Exposes pick/place/navigate primitives and world-state queries for problem_number:=1."""

    ROBOT_NAME = "robot"

    def __init__(self):
        if not rclpy.ok():
            rclpy.init()
        self._perform_action = PerformAction()
        self._world_state = WorldState()
        self._state_client = self._world_state.node.create_client(
            RequestWorldState, "/request_world_state"
        )

    def _request_world_state(self):
        future = self._state_client.call_async(RequestWorldState.Request())
        while rclpy.ok() and not future.done():
            rclpy.spin_once(self._world_state.node, timeout_sec=0.1)
        return future.result().state

    def pick(self, object_name):
        self._perform_action.do_action(ACTIONS.PICK, object_name)

    def place(self, object_name):
        self._perform_action.do_action(ACTIONS.PLACE, object_name)

    def navigate(self, location_name):
        self._perform_action.do_action(ACTIONS.NAVIGATE, location_name)

    def get_obj_names(self):
        return [obj.name for obj in self._request_world_state().objects]

    def get_location_names(self):
        return [loc.name for loc in self._request_world_state().locations]

    def get_robot_location(self):
        robots = [r for r in self._request_world_state().robots if r.name == self.ROBOT_NAME]
        return robots[0].last_visited_location if robots else None

    def shutdown(self):
        self._perform_action.node.destroy_node()
        self._world_state.node.destroy_node()


class PyRoboSimLMPWrapper:
    """Exposes the primitives of `PyRoboSimEnv` as the functions available to generated code."""

    def __init__(self, env):
        self._env = env

    def pick(self, object_name):
        self._env.pick(object_name)

    def place(self, object_name):
        self._env.place(object_name)

    def navigate(self, location_name):
        self._env.navigate(location_name)

    def get_obj_names(self):
        return self._env.get_obj_names()

    def get_location_names(self):
        return self._env.get_location_names()

    def get_robot_location(self):
        return self._env.get_robot_location()

    def say(self, message):
        print(f"robot says: {message}")


# %% [markdown]
# ## LMP prompts
#
# Few-shot examples grounded in the objects/locations of problem 1 (`snacks0` in the pantry,
# brought to the dining room `table`), and the `pick` / `place` / `navigate` primitives.

# %%
prompt_pyrobosim_ui = '''
# Python robot control script
from env_utils import pick, place, navigate, say, get_obj_names, get_location_names, get_robot_location

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# bring the soda to the table.
say('Ok - bringing the soda to the table')
navigate('soda0')
pick('soda0')
navigate('table')
place('soda0')

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# put the bread and the butter on the table.
say('Sure - putting the bread and the butter on the table')
bring_object_to_location('bread0', 'table')
bring_object_to_location('butter0', 'table')

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# throw away the waste that is on the desk.
say('Got it - throwing away the waste')
navigate('desk')
bring_object_to_location('waste', 'dumpster')

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# where are you right now.
say(f'I am currently at {get_robot_location()}')

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# what objects do you see.
say(f'I see: {", ".join(get_obj_names())}')

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# can you fly.
say('No, I can only navigate, pick and place objects')

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# how many objects are there in total.
say(f'There are {count_items(get_obj_names())} objects in total')

objects = ['bread0', 'snacks0', 'soda0', 'butter0', 'waste0', 'waste1']
locations = ['table', 'desk', 'fridge', 'pantry', 'bin', 'dumpster', 'charger']
# Bring all food items to the table.
say('Sure - bringing all food items to the table')
for obj in get_obj_names():
    if 'bread' in obj or 'snacks' in obj or 'soda' in obj or 'butter' in obj:
        bring_object_to_location(obj, 'table')

'''.strip()

# %%
prompt_fgen = '''
# define function: total = get_total(xs=numbers).
def get_total(xs):
    return sum(xs)

# define function: is_member = contains(item, items).
def contains(item, items):
    return item in items

# define function: bring_object_to_location(obj, 'dumpster').
def bring_object_to_location(obj, location):
    navigate(obj)
    pick(obj)
    navigate(location)
    place(obj)
'''.strip()

# %% [markdown]
# ## LMP config

cfg_pyrobosim = {
    "lmps": {
        "pyrobosim_ui": {
            "prompt_text": prompt_pyrobosim_ui,
            "engine": MODEL_NAME,
            "max_tokens": 512,
            "temperature": 0,
            "query_prefix": "# ",
            "query_suffix": ".",
            "stop": ["#", "objects = ["],
            "maintain_session": True,
            "debug_mode": False,
            "include_context": True,
            "has_return": False,
            "return_val_name": "ret_val",
        },
        "fgen": {
            "prompt_text": prompt_fgen,
            "engine": MODEL_NAME,
            "max_tokens": 512,
            "temperature": 0,
            "query_prefix": "# define function: ",
            "query_suffix": ".",
            "stop": ["# define", "# example"],
            "maintain_session": False,
            "debug_mode": False,
            "include_context": True,
        },
    }
}


def setup_lmp(env, cfg):
    """Builds the top-level LMP that turns natural-language commands into pick/place/navigate
    calls against `env`."""
    lmp_env = PyRoboSimLMPWrapper(env)
    fixed_vars = {}
    variable_vars = {
        k: getattr(lmp_env, k)
        for k in ["pick", "place", "navigate", "say", "get_obj_names", "get_location_names", "get_robot_location"]
    }

    lmp_fgen = LMPFGen(cfg["lmps"]["fgen"], fixed_vars, variable_vars)

    lmp_pyrobosim_ui = LMP(
        "pyrobosim_ui", cfg["lmps"]["pyrobosim_ui"], lmp_fgen, fixed_vars, variable_vars
    )
    return lmp_pyrobosim_ui


# %% [markdown]
# ## Interactive demo
#
# Run `ros2 run delib_ws_worlds run --ros-args -p problem_number:=1` in a separate terminal first.
#
# Example commands:
# * bring the snacks to the table
# * where are you right now
# * what objects do you see


def main():
    env = PyRoboSimEnv()
    lmp_pyrobosim_ui = setup_lmp(env, cfg_pyrobosim)

    print("Available objects:", env.get_obj_names())
    print("Available locations:", env.get_location_names())

    try:
        while True:
            user_input = input("Command (empty to quit): ").strip()
            if not user_input:
                break
            context = f"objects = {env.get_obj_names()}\nlocations = {env.get_location_names()}"
            try:
                lmp_pyrobosim_ui(user_input, context)
            except Exception as e:
                print(f"Command failed: {e}")
    finally:
        env.shutdown()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
