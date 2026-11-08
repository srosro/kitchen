# zoom-out

The skill for the agent who is already reaching for a file before it understands the room.

`zoom-out` is a pre-edit mapping pass. It tells the agent to stop, inspect the repo shape, follow the real paths, and name the smallest safe move before changing code.

It is not a research essay. It is a taste check before the diff.

## What It Changes

Most bad agent work starts with a premature first edit.

The agent sees a plausible file, patches a local symptom, and misses the route that actually owns the behavior. Then the session turns into cleanup.

`zoom-out` changes the opening move. Before editing, the agent has to answer:

- where does this behavior enter the system?
- who calls it?
- what data moves through it?
- what tests or checks already protect it?
- what constraints should not be violated?
- what is the smallest next action that is likely to work?

That little pause saves a lot of thrash.

## What It Looks At

- current branch and dirty state
- governing docs
- entry points
- callers and callees
- data flow
- ownership boundaries
- tests
- risks
- smallest safe next action

## Use It When

- You are in unfamiliar code.
- The change crosses layers.
- The last fix patched the wrong thing.
- You need to hand work to another agent.
- You want a map before a diff.
- The repo has duplicate paths, legacy modules, or unclear ownership.
- The request sounds simple, but the blast radius might not be.

## Example Prompts

```text
Use $zoom-out to map the checkout validation path before changing it.
```

```text
Use $zoom-out first. I want to know the real owner, the callers, and the smallest safe fix.
```

```text
Zoom out on this bug area and tell me where the implementation should happen.
```

## Good Output Feels Like

A good `zoom-out` result gives you a map, not a fog machine.

It should tell you:

- the files that matter
- the files that only look related
- the data or control flow in plain language
- the tests worth running
- the risks that deserve attention
- the next edit to make, or the reason not to edit yet

You should be able to hand the result to another agent or a human teammate and have them understand the shape of the work.

## What It Prevents

Agents are good at changing code. They are less good at knowing when they are changing the wrong code.

`zoom-out` slows the first move down so the rest of the work can go faster. It helps prevent heroic diffs, wrong-owner fixes, duplicate implementations, and "done" reports that were never grounded in the actual system.
