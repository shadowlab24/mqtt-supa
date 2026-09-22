CREATE TABLE mqtt_messages (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "received_at(UTC)" TIMESTAMPTZ NOT NULL,
    topic              TEXT        NOT NULL,
    payload            JSONB       NOT NULL
);

CREATE INDEX mqtt_messages_topic_idx       ON mqtt_messages (topic);
CREATE INDEX mqtt_messages_received_at_idx ON mqtt_messages ("received_at(UTC)");

ALTER TABLE public.mqtt_messages ENABLE ROW LEVEL SECURITY;