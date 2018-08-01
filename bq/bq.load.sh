bq --location=EU load --skip_leading_rows=1 -F "\t" --source_format=CSV g2v_draft.`date +%Y%m%d` gs://genetics-portal-data/out/v2g/*.csv ./bq.v2g.schema.json

bq --location=EU load --skip_leading_rows=1 -F "\t" --allow_jagged_rows --source_format=CSV v2d_draft.`date +%Y%m%d` gs://genetics-portal-data/out/v2d/*.csv ./bq.v2d.schema.json