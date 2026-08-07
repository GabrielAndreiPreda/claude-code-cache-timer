# How the cache timer knows what it knows

Reference for the mechanism behind `cache_timer_statusline.py`. Every claim here was
checked against Claude Code 2.1.220 and against live session data on 2026-08-07. Where a
number appears, the method that produced it is given, so you can re-run it when a future
version breaks something.

## The thing being measured

Claude Code sends the conversation prefix with each request and pays to cache it. A later
request that hits the cache reads the prefix at a discount instead of re-writing it.

Two properties drive everything else:

1. The cache has a TTL, and **every API call that hits it resets that TTL**. The clock is
   not "time since the session started" or "time since you last typed". It is time since
   the last request of any kind.
2. There are two TTLs, 5 minutes and 1 hour, and a session can use either. The 5-minute
   write costs 1.25x the base input rate, the 1-hour write costs 2x.

So the quantity worth displaying is:

```
remaining = ttl - (now - last_api_call)
```

Both terms on the right have to be discovered. Neither is handed to you.

## The clock: transcript mtime

Claude Code appends every message to
`~/.claude/projects/<slug>/<session_id>.jsonl`, assistant turns and tool results alike.
The file's mtime therefore advances on every API call the session makes.

The `<slug>` is the working directory with `/`, `\`, `:` and spaces replaced by `-`, the
leading dash preserved, so `/home/you/Projects/app` becomes `-home-you-Projects-app`.
You never need to derive it: the status line payload hands you `transcript_path`
directly.

Two other clocks were candidates and both lose to mtime:

The last record's `timestamp` field is more semantic, but it costs a tail read to obtain
and it agrees with mtime anyway. Measured on a live session, mtime age was 15.8s and the
newest record's timestamp age was 16.0s, a difference of 0.1s. Not worth the extra work.

A timestamp written by a hook was the original design and is strictly worse. See "Why
there are no hooks" below.

### Verifying the clock

```sh
python3 - <<'EOF'
import os, time
p = "<transcript path>"
print("mtime age: %.1fs" % (time.time() - os.stat(p).st_mtime))
EOF
```

Send a message and the number should drop to near zero.

## Subagents, and why they come out right for free

A subagent runs its own conversation with its own cache entry. Its API calls do not
refresh the parent's cache. If the parent is blocked waiting on one, the parent's cache is
draining the whole time.

The filesystem layout makes this self-solving:

```
~/.claude/projects/<slug>/<session_id>.jsonl                        parent, this is the clock
~/.claude/projects/<slug>/<session_id>/subagents/agent-<id>.jsonl   subagent, ignored
~/.claude/projects/<slug>/<session_id>/tool-results/                ignored
```

Subagent writes land in a subdirectory, so they never touch the parent file's mtime. Read
the parent and the stall is already reflected.


## Reading the TTL


Assistant records carry the answer under `message.usage.cache_creation`:

```json
"cache_creation": { "ephemeral_1h_input_tokens": 19627, "ephemeral_5m_input_tokens": 0 }
```

Whichever bucket is non-zero is the TTL that was written. Three details matter:

A turn that only reads the cache records `{0, 0}` in both buckets. Those records are not
an answer and have to be skipped, not treated as "unknown".

Sessions can move between TTLs mid-run. 
Re-read the value every tick rather than caching it once at startup.


If no non-zero bucket exists anywhere in the tail, the TTL is genuinely unknown and the
display says so. One exception: past 3600 seconds of inactivity the session is cold no
matter which bucket it used, so age alone settles it.

## The delivery surface: the status line

The status line is what makes the hook-free design possible. Its command receives a JSON
payload on stdin that already contains everything needed:

| Field | Use |
|---|---|
| `transcript_path` | the clock and the TTL, both |
| `model.display_name`, `workspace.current_dir` | redrawing the context a custom status line displaces |
| `session_id`, `context_window`, `cost`, ... | unused here, but present |

Configuring a status line replaces the built-in one outright; it is not merged. Everything
it used to show has to be redrawn or it is simply gone. The branch is the only piece not in
the payload, and it comes from `.git/HEAD` rather than a `git` subprocess: 7 µs against
780 µs, on a script that runs every second.

And `statusLine.refreshInterval`, minimum 1 second, re-runs the command on a timer "in
addition to the event-driven updates". The docs recommend it for exactly this case: time
based data, or a main session sitting idle while background subagents work.

Without `refreshInterval` the command runs only when a new assistant message arrives, when
`/compact` finishes, when the permission mode changes, and when vim mode toggles. 

The status line is hidden during permission dialogs, autocomplete, and the help menu. 
This cannot be changed from inside the status line.

## Why there are no hooks

**`AskUserQuestion` and permission prompts do not fire `Stop`.** The turn is not over, the
agent is mid-tool-call, and the cache drains silently while the user reads.

**`Stop` never fires for a subagent**, which emits `SubagentStop` instead. The
discriminator is `agent_id`, present inside a subagent and absent in the main loop, except
on `UserPromptSubmit`, whose payload is built without tool context and so never carries it.

**Process-tree PID detection collides constantly.** Under WSL it walks up and finds no
terminal emulator, returning 0. Every session then matches every other session. Under
tmux, in containers, and over SSH the same thing happens for different reasons. Any
cleanup logic keyed on PID deletes live sessions' state.


## Why this is not a plugin

Plugins can ship skills, agents, hooks, MCP servers, LSP servers, and monitors. A plugin
can also ship a `settings.json`, but only two keys from it are honored: `agent` and
`subagentStatusLine`.

`statusLine` is not among them, so a plugin cannot install a status line. Packaging this
as one would have added a manifest and no capability, which is why `install.py` writes to
`~/.claude/settings.json` instead.

## Known inaccuracies

The displayed number runs optimistic by up to one response duration. A cache TTL restarts
when the request is sent, but the transcript is written when the response completes, so
mtime is later than the true anchor by the length of the turn. Against an hour this is
noise. Against five minutes on a slow turn it is worth knowing.

The countdown rounds up, so the display reads `0:01` through the final second rather than
sitting at `0:00` while the cache is still alive.

During permission prompts the number is invisible, as described
above.

