# memware for Hermes Agent (experimental)

Copy this directory to `$HERMES_HOME/plugins/memware/` and select it with
`hermes config set memory.provider memware`. It shells out to the `memware`
CLI, so install memware in the same environment Hermes uses.

Hooks: `prefetch` injects currently valid beliefs before a turn;
`on_session_end` indexes the session; two tools (`memware_recall`,
`memware_remember`) are exposed to the agent.

This skeleton tracks Hermes's provider interface loosely and is not part of
memware's tested surface yet. Contributions that pin it to a Hermes release are
welcome.
