# Code as Policies Demo

This demo shows how a local LLM, served via [Ollama](https://ollama.com/), can be used to turn
natural-language commands into `pick` / `place` / `navigate` calls against the
[`delib_ws_worlds`](../../problems/delib_ws_worlds/) problem setup, following the
[Code as Policies](https://github.com/google-research/google-research/tree/master/code_as_policies)
approach.

## Prerequisites

All required software is included in the Docker container. See the [root README](../../README.md)
for setup instructions.

You also need [Ollama](https://ollama.com/) installed and running on your **host machine** (not
inside the container), with the model configured in `demo_cap.py` (`qwen2.5-coder:3b-base` by
default) pulled.

Install Ollama on the host, e.g. on Linux:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

For other platforms, see the [Ollama download page](https://ollama.com/download).

Then start the server and pull the model:

```bash
ollama serve
ollama pull qwen2.5-coder:3b-base
```

Since the container uses `network_mode: host`, the script can reach `ollama serve` on the host
at `http://localhost:11434` without any extra configuration.

## Running the Demo

Start the container and, in the first terminal, run the problem world:

```bash
docker compose run base ros2 run delib_ws_worlds run --ros-args -p problem_number:=1
```

In a second terminal, attach to the running container and run the demo script:

```bash
docker compose exec -it base bash -c "source /delib_ws/install/setup.bash && python3 src/technologies/demo_code_as_policy/demo_cap.py"
```

The script prints the available objects and locations, then prompts for commands. Example
commands for problem 1:

* `bring the snacks to the table`
* `where are you right now`
* `what objects do you see`

Enter an empty line to quit.

## Function Generation (FGen)

As in the original Code as Policies paper, generated code can call helper functions that are not
predefined. When this happens, a second LLM query (the `fgen` LMP) synthesizes that function on
the fly from its name and arguments, and it stays available for the rest of the session.

The `# how many objects are there in total.` few-shot example calls an undefined `count_items`
helper to demonstrate this. Try it with:

* `how many objects are there in total`

Watch the console: you should see `Creating function: count_items(...)` and `LMP FGEN created: ...`
before the answer is printed.
