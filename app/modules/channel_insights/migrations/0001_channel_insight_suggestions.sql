-- Suggested-question chips under the "AI Insights" box on the panel's Channel Orders page.
-- Add, hide (active = false) or reorder rows here: the panel picks them up within a minute, no release needed.
CREATE TABLE channel_insight_suggestions (
    id          serial PRIMARY KEY,
    question    text NOT NULL CHECK (length(btrim(question)) BETWEEN 1 AND 300),
    sort_order  integer NOT NULL DEFAULT 0,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX channel_insight_suggestions_question_idx ON channel_insight_suggestions (lower(btrim(question)));

INSERT INTO channel_insight_suggestions (question, sort_order) VALUES ('Which orders are high risk?', 1);
