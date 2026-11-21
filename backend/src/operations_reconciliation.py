"""Additional operational reconciliation rules for this project."""

RECONCILIATION_VIEWS = {
    'blend-panel-coverage': (
        '拼配原料品评覆盖',
        '逐配方核对有占比的原料批是否具备品评，将缺少原料品评的配方交给品控补测。',
        ['配方', '修订号', '原料批', '来源', '占比', '原料品评次数', '最近品评', '补测建议'],
        """
SELECT r.code,r.revision,l.code,l.origin,s.percent,COUNT(p.id),MAX(p.created_at),
       CASE WHEN COUNT(p.id)=0 THEN '待补原料品评' ELSE '原料品评已登记' END
FROM blend_share s JOIN blend_recipe r ON r.id=s.recipe_id JOIN tea_lot l ON l.id=s.lot_id
LEFT JOIN sensory_panel p ON p.lot_id=l.id AND p.superseded_at IS NULL AND p.invalidated_at IS NULL
WHERE s.percent>0
GROUP BY r.code,r.revision,l.code,l.origin,s.percent ORDER BY COUNT(p.id),r.code,l.code
""",
    ),
    'grade-basis-lineage': (
        '判级依据版本链',
        '逐审核单列出冻结评分依据的各个版本，核对评分更正前后的原料评分与所选审评单。',
        ['配方', '审核单', '依据版本', '来源', '生效状态', '原料批', '占比', '审评单', '香气', '汤体', '回甘', '叶底', '杂味扣分'],
        """
SELECT recipe.code,r.id,e.generation+1,e.cause,
       CASE WHEN e.superseded_at IS NULL THEN '当前生效' ELSE '已被取代' END,
       i.lot_code,i.share_pct,i.panel_id,i.aroma,i.body,i.sweetness,i.leaf,i.defect
FROM grade_evidence e JOIN grade_review r ON r.id=e.review_id
JOIN blend_recipe recipe ON recipe.id=r.recipe_id
JOIN JSON_TABLE(e.snapshot,'$.ingredients[*]' COLUMNS (
    lot_code VARCHAR(40) PATH '$.lot', share_pct DECIMAL(5,2) PATH '$.percent',
    panel_id INTEGER PATH '$.panel_id', aroma INTEGER PATH '$.scores.aroma',
    body INTEGER PATH '$.scores.body', sweetness INTEGER PATH '$.scores.sweetness',
    leaf INTEGER PATH '$.scores.leaf', defect INTEGER PATH '$.scores.defect'
)) i ON TRUE
ORDER BY r.id DESC,e.generation DESC,i.lot_code
""",
    ),
}
