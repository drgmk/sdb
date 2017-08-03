mysql sdb_samples -N -e "SHOW TABLES;" | while read t
do
    ./db-insert-many.sh $t
    # echo $st
done
