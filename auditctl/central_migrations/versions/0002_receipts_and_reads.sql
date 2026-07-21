CREATE TABLE __SCHEMA__.ingest_receipt (
    receipt_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    observation_id      bigint      NOT NULL UNIQUE,
    origin_stream_id    uuid        NOT NULL,
    origin_seq          bigint      NOT NULL,
    event_id            text        NOT NULL UNIQUE,
    record_sha256       text        NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    duplicate_count     bigint      NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    first_received_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ingest_receipt_observation_fk
        FOREIGN KEY (observation_id)
        REFERENCES __SCHEMA__.ingest_observation(observation_id),
    CONSTRAINT ingest_receipt_origin_seq_key
        UNIQUE (origin_stream_id, origin_seq)
);

INSERT INTO __SCHEMA__.ingest_receipt (
    observation_id,
    origin_stream_id,
    origin_seq,
    event_id,
    record_sha256,
    first_received_at,
    last_seen_at
)
SELECT
    observation_id,
    origin_stream_id,
    origin_seq,
    event_id,
    record_sha256,
    ingested_at,
    ingested_at
FROM __SCHEMA__.ingest_observation
ON CONFLICT (observation_id) DO NOTHING;

CREATE INDEX ingest_observation_type_offset_idx
    ON __SCHEMA__.ingest_observation (event_type, ingest_offset DESC);
CREATE INDEX ingest_observation_stream_offset_idx
    ON __SCHEMA__.ingest_observation (origin_stream_id, ingest_offset DESC);

CREATE TABLE __SCHEMA__.schema_principal (
    role_kind  text        PRIMARY KEY CHECK (role_kind IN ('migration', 'runtime')),
    role_name  name        NOT NULL UNIQUE,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
