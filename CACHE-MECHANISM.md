# How the cache timer knows what it knows

Reference for the mechanism behind `src/cache_timer/statusline.py`. Every claim here was
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

## The clock: the last assistant turn's timestamp

Claude Code appends every message to
`~/.claude/projects/<slug>/<session_id>.jsonl`, assistant turns and tool results alike.
Each record carries a `timestamp` field, an ISO 8601 string in UTC to the millisecond:

```json
"timestamp": "2025-01-31T09:15:42.318Z"
```

The `<slug>` is the working directory with `/`, `\`, `:` and spaces replaced by `-`, the
leading dash preserved, so `/home/you/Projects/app` becomes `-home-you-Projects-app`.
You never need to derive it: the status line payload hands you `transcript_path`
directly.

### Why not the newest record

The newest record is not the last API call. A transcript logs everything the session
did, and most of what a session does is local: your own turns, edits, attachments,
interface state. Many of those records carry a timestamp, and none of them means a
request went out.

A slash command is the clearest case: running `/exit` or `/clear` appends a timestamped
record and makes no request at all, so a clock keyed to the newest record restarts on a
cache nothing touched. Your own turn is the same problem in slower motion: it is
written when you hit enter, before the request it will eventually trigger.

The marker that does hold is `message.usage`. Only assistant turns carry one, and only
a response from the API produces one, so it cannot appear without a call having been
made. The clock is therefore the newest record where `type` is `assistant` and
`message.usage` is present; every other record is skipped.

Two properties make this durable. It is an allowlist, so a record type added to Claude
Code in future is ignored by default rather than silently becoming a new way to reset
the clock. And when it is wrong it is wrong in the safe direction: a missed marker
anchors further back and understates the remaining cache, where the newest-record clock
overstated it. Understating costs a cache write you did not have to pay for; overstating
tells you a cold cache is warm.

### Why not the file's mtime

The mtime is free, and on a live session it agrees: measured mid-conversation, mtime age
was 15.8s against 16.0s for the newest record. That agreement is what made mtime look
like the better clock, and it does not hold once a session goes quiet.

Claude Code touches transcript files long after their last API call. A session whose
newest record is days old can carry an mtime from minutes ago, and an mtime clock reads
that as most of a 1-hour cache still remaining on a cache that went cold days earlier.
Every touch restarts the countdown from full, so the failure is silent and the number it
produces looks plausible.

Reading the timestamp costs nothing extra in practice. The tail is already being read to
find the TTL, and one backwards walk answers both questions.

### Verifying the clock

This compares all three clocks across every transcript you have. A row where mtime
disagrees by hours is a session an mtime countdown would have lied about; a non-zero
last column is a session the newest-record countdown would have lied about, by that
many seconds of cache already spent. The rows to look at are the sessions that ended on a
local record, which is the common case, since local records are written after the last
response.

```sh
python3 - <<'EOF'
import glob, json, os, time
now = time.time()
for path in sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))):
    newest = api = None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            stamp = record.get("timestamp")
            if not stamp:
                continue
            newest = stamp
            message = record.get("message")
            if record.get("type") == "assistant" and isinstance(message, dict) \
                    and isinstance(message.get("usage"), dict):
                api = stamp
    print("mtime %7.2fh ago   newest %s   api %s   overstated by %s" %
          ((now - os.path.getmtime(path)) / 3600, newest, api,
           "?" if not (newest and api) else
           "%.1fs" % ((time.mktime(time.strptime(newest[:19], "%Y-%m-%dT%H:%M:%S"))
                       - time.mktime(time.strptime(api[:19], "%Y-%m-%dT%H:%M:%S"))))))
EOF
```

On a session you are actively using, all three should be current.

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

Subagent writes land in a subdirectory, so they add no record to the parent file. Read the
parent and the stall is already reflected.

## Reading the TTL

Assistant records carry the answer under `message.usage.cache_creation`:

```json
"cache_creation": { "ephemeral_1h_input_tokens": 19627, "ephemeral_5m_input_tokens": 0 }
```

Whichever bucket is non-zero is the TTL that was written. Three details matter.

A turn that only reads the cache records `{0, 0}` in both buckets. Those records are not
an answer and have to be skipped, not treated as "unknown".

Sessions can move between TTLs mid-run, so the value has to be re-read every tick rather
than cached once at startup.

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

The status line is hidden during permission dialogs, autocomplete, and the help menu, and
nothing inside the status line can change that.

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
as one would have added a manifest and no capability, which is why
`claude-cache-timer install` writes to `~/.claude/settings.json` instead.

## Known inaccuracies

The displayed number runs optimistic by up to one response duration. A cache TTL restarts
when the request is sent, but the record is written when the response completes, so its
timestamp is later than the true anchor by the length of the turn. Against an hour this is
noise. Against five minutes on a slow turn it is worth knowing.

The countdown rounds up, so the display reads `0:01` through the final second rather than
sitting at `0:00` while the cache is still alive.

During permission prompts the number is invisible, as described above.
