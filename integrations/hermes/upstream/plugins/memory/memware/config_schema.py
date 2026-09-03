"""memware's declared config surface — rendered by the generic desktop panel."""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="memware",
    label="memware",
    docs_url="https://github.com/ericwalisko/memware/blob/main/docs/design.md",
    fields=(
        ProviderField(
            key="db_path",
            label="Database path",
            kind=KIND_TEXT,
            default="$HERMES_HOME/memware/memware.db",
            description="SQLite file holding the belief ledger and transcript index.",
            info=(
                "Profile-scoped by default. Point several clients at one path "
                "(for example ~/.memware/memware.db) to share one memory."
            ),
            inline=True,
        ),
        ProviderField(
            key="prefetch_k",
            label="Beliefs per turn",
            kind=KIND_NUMBER,
            default="6",
            description="How many currently valid beliefs are injected before each turn.",
            info="Set to 0 to disable injection and rely on the memware_recall tool alone.",
            inline=True,
        ),
        ProviderField(
            key="auto_sync",
            label="Index turns",
            kind=KIND_BOOL,
            default="true",
            description="Index every completed turn so it can be recalled later.",
            inline=True,
        ),
    ),
)
