-- Single-call drill-down header: scores + summary + sentiment + transcript + links.
-- Metabase: native question with a required {{call_id}} parameter; put it on a
-- dashboard that the flagged-queue / scorecard click-throughs filter.
SELECT
    cs.call_id,
    cs.scored_at::date            AS scored_on,
    cs.manager_name,
    cs.department_name,
    cs.direction,
    cs.started_at,
    ROUND(cs.duration_sec / 60.0, 1) AS minutes,
    cs.percent,
    cs.zone,
    cs.target_status,
    cs.sentiment_customer,
    cs.sentiment_agent,
    cs.summary,
    cs.strengths,
    cs.growth_zone,
    cs.training_recommendation,
    cs.red_flags,
    ca.language,
    -- Open the contact/lead (and its call recording) in Bitrix:
    CASE ca.crm_entity_type
        WHEN 'CONTACT' THEN 'https://amanat.bitrix24.kz/crm/contact/details/' || ca.crm_entity_id || '/'
        WHEN 'COMPANY' THEN 'https://amanat.bitrix24.kz/crm/company/details/' || ca.crm_entity_id || '/'
        WHEN 'LEAD'    THEN 'https://amanat.bitrix24.kz/crm/lead/details/'    || ca.crm_entity_id || '/'
        WHEN 'DEAL'    THEN 'https://amanat.bitrix24.kz/crm/deal/details/'    || ca.crm_entity_id || '/'
    END                           AS bitrix_crm_url,
    ca.audio_object_key,          -- object-storage key (presign for playback)
    t.full_text                   AS transcript
FROM call_scores_latest cs
JOIN calls ca       ON ca.id = cs.call_id
LEFT JOIN transcripts t ON t.call_id = cs.call_id
WHERE cs.call_id = {{call_id}};
