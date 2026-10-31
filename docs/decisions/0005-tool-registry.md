# 0005: Tool registry

Status: accepted

## Why must every tool call go through one registry instead of calling functions directly?
ToolRegistry.call(agent, tool_name, args) is the only way an agent can
run a tool. Every call passes the same gate, in the same order:
tool exists → agent has the capability → arguments validate → run
handler → cap output.

- Checks can't be forgotten: if agents called tool functions directly,
  every call site would have to remember the permission check, and one
  missed spot would be a security hole. With one door, the check lives
  in exactly one place.
- One place to extend: later safety features (policy rules, risk
  levels, human approvals, audit logging in M5) plug into this single
  method instead of being added to every tool.
- Consistent errors: every failure becomes a typed error (UnknownTool,
  ToolPermissionError, ToolInputError, ToolError) that the agent loop
  handles the same way.

This is the same pattern as the LLM gateway (ADR 0003): one choke point
for a risky, external operation.

## Why deny by default, and why is the permission check before argument validation?
Deny by default: a tool declares the capability it needs (for example
web.search), and an agent may use it only if that exact string is in
its capabilities list in Postgres. An agent with no capabilities can do
nothing. Adding a new tool never silently grants access to existing
agents; someone must grant it explicitly. Mistakes therefore fail
closed (a blocked call) instead of open (an unexpected action).

Permission before validation: if arguments were validated first, an
unauthorized agent could send bad arguments and read the error messages
to learn the tool's schema, which leaks information about a tool it
isn't allowed to use. Checking permission first means an unauthorized
agent learns only "not allowed." It also avoids doing any work for a
request that will be rejected anyway.

## Why does each tool have a Pydantic input model, and who else uses its schema?
The model does two jobs from one definition:
- Validation: LLM-generated arguments are untrusted input (same idea as
  ADR 0004). Pydantic checks types, required fields, and limits (for
  example max_results ≤ 10) before the handler runs, so handlers can
  trust their inputs.
- Description: describe_for exports the model's JSON schema, which we
  show to the LLM so it knows which arguments exist and what values are
  allowed.

Because the schema the LLM sees and the rules the registry enforces
come from the same class, they can't drift apart. Validation errors are
also precise enough for the model to fix its call on the next step
("max_results: Input should be less than or equal to 10").

## Why does the unknown-tool error list only the tools that agent may use?
Listing valid options helps the model recover, since a typo like
"websearch" can be fixed when it sees "web_search". But listing every
registered tool would leak what exists, including tools the agent isn't
permitted to use, and could tempt a manipulated model into trying them.
So the error is useful (shows real options) without revealing anything
beyond the agent's own permissions. This keeps deny-by-default
consistent: tools you can't use are effectively invisible to you.

## Why truncate tool output, and what does the "external content" line in search results NOT protect against?
Truncation: tool output goes straight into the LLM's context. A tool
returning megabytes of text could overflow the context window, raise
the token bill (ADR 0003 tracks this cost), or slow every later step.
The registry caps output at 8,000 characters and adds a visible marker,
so the model knows the result was cut off instead of assuming it's
complete. Doing this in the registry protects against every tool,
including ones written later.

The external-content line: search results are text written by
strangers on the web, so they're untrusted. The line tells the model to
treat them as data, not instructions. But it does NOT prevent prompt
injection. It's only more text in the prompt, and a web page saying
"ignore previous instructions and ..." can still persuade the model.
LLMs have no reliable boundary between data and instructions.

Real protection comes from limiting what a fooled agent can do:
- Capabilities and deny by default: a researcher that only holds
  web.search can't delete files or send email, even if injected text
  asks it to.
- The single registry gate: every action is still checked, whatever
  the model was convinced to try.
- Later (M5): risk levels, human approval for dangerous tools, and
  audit logs.

The label lowers the risk; permissions limit the damage.