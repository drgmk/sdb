#!/bin/sh

curl "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20id1.id%20as%20main,id2.id%20as%20xid%20FROM%20ident%20AS%20id1%20JOIN%20ident%20AS%20id2%20USING(oidref)%20WHERE%20id1.id=%27$1%27" | stilts -Xms1g -Xmx1g -classpath /Library/Java/Extensions/mysql-connector-java-5.1.8-bin.jar -Djdbc.drivers=com.mysql.jdbc.Driver tpipe in=- ifmt=votable cmd='random' omode=tosql protocol=mysql db=sandbox dbtable=xid write=dropcreate user=grant password=grant
