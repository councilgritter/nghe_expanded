-- 0002_english_glosses.sql — English definitions alongside the Vietnamese ones.
--
-- Why a migration rather than a column rename: 0001 stored a single
-- `tokens.definition`, filled from the model's Vietnamese pre-teach gloss.  The
-- reader now shows an English meaning from the local Việt→Anh dictionary first and
-- the Vietnamese gloss underneath, so the two have to be stored separately and
-- survive independently:
--
--   definition_en   local dictionary gloss (tools/build_glosses.py), English
--   definition_vi   model pre-teach gloss, Vietnamese
--   senses_en       JSON array of further English senses, for the sheet
--   definition      unchanged: the primary string to display, which is
--                   definition_en when there is one and definition_vi otherwise
--
-- `preteach.gloss_en` does the same job for the pre-teach panel, where the term is
-- a word rather than a span.
--
-- Additive only, per the module's rule: nothing here drops or rewrites a column.

ALTER TABLE tokens ADD COLUMN definition_en TEXT;
ALTER TABLE tokens ADD COLUMN definition_vi TEXT;
ALTER TABLE tokens ADD COLUMN senses_en TEXT;

ALTER TABLE preteach ADD COLUMN gloss_en TEXT;
