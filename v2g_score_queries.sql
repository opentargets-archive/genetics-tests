SELECT
    count(qtl_pval),
    min(qtl_pval),
    gene_id,
    gene_name
FROM ot.v2g
WHERE (chr_id = '16') AND (variant_id = '16_53800954_T_C') AND (type_id = 'eqtl')
GROUP BY
    gene_id,
    gene_name

SELECT
    count(interval_score),
    max(interval_score),
    gene_id,
    gene_name
FROM ot.v2g
WHERE (chr_id = '16') AND (variant_id = '16_53800954_T_C') AND (type_id = 'pchic')
GROUP BY
    gene_id,
    gene_name
