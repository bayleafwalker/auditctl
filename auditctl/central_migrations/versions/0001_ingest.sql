CREATE TABLE __SCHEMA__.ingest_stream (
    origin_stream_id       uuid        PRIMARY KEY,
    highest_contiguous_seq bigint      NOT NULL DEFAULT 0
                                      CHECK (highest_contiguous_seq >= 0),
    created_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at             timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE __SCHEMA__.ingest_observation (
    observation_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingest_offset      bigint      GENERATED ALWAYS AS IDENTITY NOT NULL UNIQUE,
    origin_stream_id   uuid        NOT NULL,
    origin_seq         bigint      NOT NULL CHECK (origin_seq > 0),
    event_id           text        NOT NULL UNIQUE,
    schema_version     integer     NOT NULL CHECK (schema_version = 1),
    record_class       text        NOT NULL CHECK (record_class = 'observation'),
    event_type         text        NOT NULL CHECK (event_type <> ''),
    actor              text        NOT NULL CHECK (actor <> ''),
    runtime_session_id text,
    occurred_at        timestamptz NOT NULL,
    basis_revision     text,
    correlation_id     text,
    causation_id       text,
    payload            jsonb       NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256     text        NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    record_sha256      text        NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    producer_created_at timestamptz NOT NULL,
    ingested_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ingest_observation_origin_fk
        FOREIGN KEY (origin_stream_id)
        REFERENCES __SCHEMA__.ingest_stream(origin_stream_id),
    CONSTRAINT ingest_observation_origin_seq_key
        UNIQUE (origin_stream_id, origin_seq)
);

CREATE INDEX ingest_observation_occurred_offset_idx
    ON __SCHEMA__.ingest_observation (occurred_at DESC, ingest_offset DESC);
