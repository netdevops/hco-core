# Admin documentation

This part is for the person who installs and operates hco-core.

1. [Install](install.md): requirements, the package, and the development stack.
2. [Configuration](configuration.md): the `HCO_` settings, the validation key, and logs.
3. [Database](database.md): Postgres, the schema upgrade, and the checkpointer tables.
4. [Source of truth](source-of-truth.md): the YAML inventory and Nautobot.
5. [Secrets](secrets.md): device credentials from environment variables.
6. [Device connections](connections.md): the mock connection and scrapli.
7. [Security](security.md): the actor header, network exposure, and keys.

hco-core does not write to a device in Phase 1. A run stops at approval.
