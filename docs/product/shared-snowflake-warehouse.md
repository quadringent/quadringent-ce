# Use an existing Snowflake warehouse

Declare site.snowflakeWarehouse in Helm values, or
QUADRINGENT_SNOWFLAKE_WAREHOUSE locally. The verifier role needs USAGE on it.

An explicit warehouse is managed outside Quadringent. Installation, pause and
rollback neither create nor suspend it. Table, pipe and role names remain unchanged.
Its owner controls sizing and autosuspend. Without this setting, the dedicated
warehouse behavior remains.

Shared-warehouse metering does not measure Quadringent's own cost. The collector
reports SharedWarehouseAttributionRequired without money or an artificial zero.
User/query-tag attribution is required to price Quadringent queries; it alone does
not allocate shared idle credits or cloud services.
