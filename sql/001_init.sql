CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_sha256 char(64) NOT NULL UNIQUE,
    original_geojson bytea NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'validating', 'done', 'failed')),
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS results (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    feature_index integer NOT NULL CHECK (feature_index >= 0),
    farm_id text,
    valid boolean NOT NULL,
    codes text[] NOT NULL DEFAULT '{}',
    messages text[] NOT NULL DEFAULT '{}',
    UNIQUE (job_id, feature_index)
);

CREATE INDEX IF NOT EXISTS results_job_farm_id_idx ON results (job_id, farm_id);
