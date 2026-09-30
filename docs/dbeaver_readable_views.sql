-- ============================================================================
-- AgentMeter — readable queries for DBeaver
-- ----------------------------------------------------------------------------
-- The raw tables store bare codes and numbers (correct = 0/1, times in seconds,
-- long model paths). These queries relabel and round everything into a grid you
-- can read at a glance. They are READ-ONLY SELECTs — they never modify your data.
--
-- HOW TO USE
--   * Sections A and B: open a new SQL editor on the matching connection
--     (A = the study DB, B = the app DB) and run any query.
--   * Section C: joins across BOTH files. Open a SQL editor on a NEW, EMPTY
--     SQLite database (create one, e.g. agentmeter_report.db), edit the two
--     ATTACH paths to your machine, run the ATTACH lines once, then the queries.
--   * Section D (optional): turns these into permanent VIEWS you can browse in
--     the navigator. Re-run the ATTACH lines after each reconnect.
-- ============================================================================


-- ############################################################################
-- SECTION A — run on the STUDY connection (agentmeter_full_l4.db)
-- ############################################################################

-- A1. Per-scenario verdicts (short model name, decoded verdict, rounded units)
SELECT
    substr(model, instr(model, '/') + 1)                    AS model,
    scenario_id                                             AS scenario,
    predicted_label                                         AS predicted,
    held_out_label                                          AS actual,
    CASE correct WHEN 1 THEN 'correct' ELSE 'wrong' END     AS verdict,
    round(scenario_total_time_s, 3)                         AS total_time_s,
    round(scenario_peak_vram_mb, 1)                         AS peak_vram_mb
FROM scenario_results
ORDER BY model, scenario_id;

-- A2. Accuracy per model (how often each model was correct)
SELECT
    substr(model, instr(model, '/') + 1)                    AS model,
    COUNT(*)                                                AS scenarios,
    SUM(correct)                                            AS correct,
    round(100.0 * SUM(correct) / COUNT(*), 1)              AS accuracy_pct,
    round(AVG(scenario_total_time_s), 3)                   AS avg_time_s,
    round(AVG(scenario_peak_vram_mb), 1)                   AS avg_peak_vram_mb
FROM scenario_results
GROUP BY model
ORDER BY accuracy_pct DESC;

-- A3. Per-agent cost, averaged across scenarios (which agent is expensive?)
SELECT
    substr(model, instr(model, '/') + 1)                    AS model,
    agent_name                                             AS agent,
    round(AVG(wall_time_s), 4)                             AS avg_wall_s,
    round(AVG(ttft_s), 4)                                  AS avg_ttft_s,
    round(AVG(vram_delta_mb), 1)                           AS avg_vram_delta_mb,
    round(AVG(input_tokens), 0)                            AS avg_in_tokens,
    round(AVG(output_tokens), 0)                           AS avg_out_tokens
FROM agent_metrics
GROUP BY model, agent_name
ORDER BY model,
    CASE agent_name WHEN 'perceive' THEN 1 WHEN 'reason' THEN 2
                    WHEN 'decide' THEN 3 WHEN 'act' THEN 4 ELSE 5 END;

-- A4. Run overview
SELECT run_id, status, quant_setting AS quant, hardware_label AS hardware,
       started_at, finished_at
FROM runs
ORDER BY started_at DESC;


-- ############################################################################
-- SECTION B — run on the APP connection (agentmeter_app.db)
-- ############################################################################

-- B1. Saved sessions, with the preset name resolved and tags listed
SELECT
    s.id                                                   AS session_id,
    s.name                                                 AS session_name,
    p.name                                                 AS scored_with_preset,
    s.source_run_id                                        AS from_study_run,
    s.created_at                                           AS created,
    (SELECT group_concat(t.name, ', ')
       FROM session_tag st JOIN tag t ON t.id = st.tag_id
      WHERE st.session_id = s.id)                          AS tags
FROM session s
LEFT JOIN weight_preset p ON p.id = s.preset_id
ORDER BY datetime(s.created_at) DESC;

-- B2. Weight presets (built-in vs user), weights shown as one row each
SELECT
    name,
    CASE is_builtin WHEN 1 THEN 'built-in' ELSE 'user' END AS kind,
    w_accuracy AS w_acc, w_latency AS w_lat, w_vram, w_tokens AS w_tok
FROM weight_preset
ORDER BY is_builtin DESC, name;

-- B3. Cached Hugging Face metadata (now readable from typed columns)
SELECT
    substr(model_id, instr(model_id, '/') + 1)             AS model,
    status,
    params_b                                               AS size_b_params,
    downloads, likes, license, pipeline_tag                AS task,
    datetime(fetched_at, 'unixepoch')                      AS fetched
FROM hf_metadata_cache
ORDER BY downloads DESC;


-- ############################################################################
-- SECTION C — cross-file joins (run on a NEW empty SQLite DB)
-- Edit the two paths, run the ATTACH lines once per connection, then query.
-- ############################################################################

-- ATTACH DATABASE 'C:/Users/ASUS_USER/AgentMeter/AgentMeter/results/agentmeter_full_l4.db' AS study;
-- ATTACH DATABASE 'C:/Users/ASUS_USER/AgentMeter/AgentMeter/results/agentmeter_app.db'      AS app;

-- C1. Each saved session next to the study run it was imported from
-- SELECT
--     s.name                                                AS session_name,
--     s.source_run_id                                       AS study_run,
--     r.status                                              AS run_status,
--     r.hardware_label                                      AS hardware,
--     CASE WHEN r.run_id IS NULL THEN 'run not present locally'
--          ELSE 'resolved' END                              AS link_state
-- FROM app.session s
-- LEFT JOIN study.runs r ON r.run_id = s.source_run_id
-- ORDER BY s.name;


-- ############################################################################
-- SECTION D (optional) — make Section A/B queries permanent VIEWS
-- Run on a report DB after the Section C ATTACH lines. Browse them in the
-- navigator like tables. NOTE: re-run the ATTACH lines after each reconnect,
-- or the views that read study./app. will error until the files are attached.
-- ############################################################################

-- DROP VIEW IF EXISTS v_scenario_results;
-- CREATE VIEW v_scenario_results AS
-- SELECT substr(model, instr(model,'/')+1) AS model, scenario_id AS scenario,
--        predicted_label AS predicted, held_out_label AS actual,
--        CASE correct WHEN 1 THEN 'correct' ELSE 'wrong' END AS verdict,
--        round(scenario_total_time_s,3) AS total_time_s,
--        round(scenario_peak_vram_mb,1) AS peak_vram_mb
-- FROM study.scenario_results;

-- DROP VIEW IF EXISTS v_model_accuracy;
-- CREATE VIEW v_model_accuracy AS
-- SELECT substr(model, instr(model,'/')+1) AS model, COUNT(*) AS scenarios,
--        SUM(correct) AS correct,
--        round(100.0*SUM(correct)/COUNT(*),1) AS accuracy_pct
-- FROM study.scenario_results GROUP BY model;
