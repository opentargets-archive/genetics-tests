## output intervals surrounding
#!/usr/bin/env bash

WINDOW=$((500000/2))
while read -r line ; do
    array=($line)
    echo clickhouse-client --query="select * from ot.v2g where chr_id = '${array[2]}' and position between ${array[3]}-$WINDOW and ${array[3]}+$WINDOW into outfile './v2g.${array[0]}.neighboorhood.csv'"
done < gold_standard_rsid_positions.tsv

