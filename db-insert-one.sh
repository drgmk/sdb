#!/bin/sh

# get info on a star from vizier catalogues and put them in mysql

# script takes name as first arg, which will be looked for in sesame and simbad. if it's
# not going to exist then give coords as second arg (ra,dec) and the looking for will be
# skipped, as will the addition of any cross-ids

# some filenames
fp=/tmp/ppmxl$RANDOM.xml
ft=/tmp/tmp$RANDOM.xml

# some other knobs
db=sed_db
user=grant
pass=grant
mode=append

# the stilts command, removed -Xms1g -Xmx1g since presumably little memory needed
stilts='/Applications/stilts -classpath /Library/Java/Extensions/mysql-connector-java-5.1.8-bin.jar -Djdbc.drivers=com.mysql.jdbc.Driver'

# sesame appears to be more picky than simbad, for example "tau cet" won't work with this script yet

# get ra,dec from sesame, these are corrected to epoch 2000.0 where possible so will match with ppmxl
if [ $# -eq 1 ]
then
     co=`sesame $1 | egrep -w 'jradeg|jdedeg' | sed s/\<jradeg\>// | sed s/\<\\\\/jradeg\>// | sed s/\<jdedeg\>// | sed s/\<\\\\/jdedeg\>//` 
     cojoin=`echo $co | sed "s/ /,/"`
     #cojoin=279.234734787,38.783688956 # Vega
     ra=`echo $cojoin | sed 's/\(.*\),.*/\1/'`
     de=`echo $cojoin | sed 's/.*,\(.*\)/\1/'`
else
    cojoin=$2
fi

# grab closest PPMXL entry (within 2"), all columns
#ppout=`vizquery -mime=votable -source=ppmxl -c.rs=2 -out.all -out.max=1 -out.add=_r -c="$cojoin"`
vizquery -mime=votable -source=ppmxl -c.rs=2 -sort=_r -out.all -out.max=1 -out.add=_r -c="$cojoin" | grep -v "^#" > $fp
# grab the PPMXL id
ppmxl=`$stilts tpipe in=$fp ifmt=votable cmd='random' cmd='keepcols ppmxl' out=- ofmt=csv-noheader`
echo $ppmxl
if [[ $ppmxl = '' ]]
then
    echo "$1 not found"
    exit
fi

# see if we have it already
res=$(mysql -u$user -p$pass $db -N -e "SELECT ppmxl FROM ppmxl WHERE ppmxl='$ppmxl';")
if [[ $res = $ppmxl ]]
then
    echo "have $1 (PPMXL $ppmxl) already"
#    exit
fi

# put PPMXL entry in mysql
$stilts tpipe in=$fp ifmt=votable cmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=ppmxl write=$mode

# get crossids from simbad, put PPMXL as main id, and add in an xid too
if [ $# -eq 1 ]
then
    cid=`echo $1 | sed 's/ /%20/g' | sed 's/+/%2B/g'`
    echo $cid
    curl -s "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20$ppmxl%20as%20ppmxl,id2.id%20as%20xid%20FROM%20ident%20AS%20id1%20JOIN%20ident%20AS%20id2%20USING(oidref)%20WHERE%20id1.id=%27$cid%27" | $stilts tpipe in=- ifmt=votable cmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=xids write=$mode
fi
echo ppmxl,xid > $ft
echo $ppmxl,$ppmxl >> $ft
echo $ppmxl,$1 >> $ft
$stilts tpipe in=$ft ifmt=csv cmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=xids write=append

#exit

# add IRAS FSC cross id, always append
vizquery -mime=votable -source=ppmxl -c.rs=2 -sort=_r -out.max=1 -c="$cojoin" -out="_RA(J2000,1983.5)" -out.add="e_RAJ2000" -out.add="e_DEJ2000" -out.add="PPMXL" | grep -v "^#" > $fp
$stilts sqlclient db='jdbc:mysql://localhost/photometry' user='grant' password='grant' sql="SELECT * from iras_fsc where _raj2000 between $ra-120/3600.0 and $ra+120/3600.0 and _dej2000 between $de-120/3600.0 and $de+120/3600.0" ofmt=votable > $ft
$stilts tmatch2 in1=$fp ifmt1=votable in2=$ft ifmt2=votable ocmd='keepcols "PPMXL IRAS_ID"' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=xids find=best write=append matcher=skyellipse values1='_RAJ2000 _DEJ2000 e_RAJ2000/1e3 e_DEJ2000/1e3 0.0' values2='_RAJ2000 _DEJ2000 Major Minor PosAng' params='20'

# and IRAS PSC cross id, always append
$stilts sqlclient db='jdbc:mysql://localhost/photometry' user='grant' password='grant' sql="SELECT * from iras_psc where _raj2000 between $ra-120/3600.0 and $ra+120/3600.0 and _dej2000 between $de-120/3600.0 and $de+120/3600.0" ofmt=votable > $ft
$stilts tmatch2 in1=$fp ifmt1=votable in2=$ft ifmt2=votable ocmd='keepcols "PPMXL IRAS_ID"' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=xids find=best write=append matcher=skyellipse values1='_RAJ2000 _DEJ2000 e_RAJ2000/1e3 e_DEJ2000/1e3 0.0' values2='_RAJ2000 _DEJ2000 Major Minor PosAng' params='20'

# get star info from simbad
curl -s "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20basic.main_id,sp_type,sp_bibcode,plx_value,plx_err,plx_bibcode%20FROM%20basic%20JOIN%20ident%20ON%20oidref%20=%20oid%20WHERE%20id=%27$cid%27" > $ft
$stilts tjoin nin=2 in1=$fp ifmt1=votable icmd1='keepcols ppmxl' in2=$ft ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=simbad write=$mode

#### now do catalogues we're too lazy to match or collect ourselves. these are sorted
#### roughly in wavelength order ####

# Tycho-2, need to query against 1991.25 position
coty=`vizquery -mime=votable -source=ppmxl -c.rs=2 -sort=_r -out.max=1 -c="$cojoin" -out="_RA(J2000,1991.25)" | grep "<TR><TD>" | sed s/\<TR\>\<TD\>// | sed s/\<\\\\/TD\>\<TD\>/,/ | sed s/\<\\\\/TD\>\<\\\\/TR\>//`
vizquery -mime=votable -source=I/259/tyc2 -c.rs=2 -sort=_r -out.max=1 -out.add=_r -out.add=e_BTmag -out.add=e_VTmag -out.add=prox -out.add=ccdm -c="$coty" | grep -v "^#" > $ft
$stilts tjoin nin=2 in1=$fp ifmt1=votable icmd1='keepcols ppmxl' in2=$ft ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=tyc2 write=$mode

# 2MASS, mean epoch of 1999.3, midway through survey 2006AJ....131.1163S
cotm=`vizquery -mime=votable -source=ppmxl -c.rs=2 -sort=_r -out.max=1 -c="$cojoin" -out="_RA(J2000,1999.3)" | grep "<TR><TD>" | sed s/\<TR\>\<TD\>// | sed s/\<\\\\/TD\>\<TD\>/,/ | sed s/\<\\\\/TD\>\<\\\\/TR\>//`
vizquery -mime=votable -source=2mass -c.rs=2 -sort=_r -out.max=1 -out.add=_r -c="$cotm" | grep -v "^#" > $ft
$stilts tjoin nin=2 in1=$fp ifmt1=votable icmd1='keepcols ppmxl' in2=$ft ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=2mass write=$mode

# ALLWISE, shift ppmxl coordinates to 2010.3, midway through cryo lifetime
cowise=`vizquery -mime=votable -source=ppmxl -c.rs=2 -sort=_r -out.max=1 -c="$cojoin" -out="_RA(J2000,2010.3)" | grep "<TR><TD>" | sed s/\<TR\>\<TD\>// | sed s/\<\\\\/TD\>\<TD\>/,/ | sed s/\<\\\\/TD\>\<\\\\/TR\>//`
vizquery -mime=votable -source=II/328/allwise -out.add=_r -c.rs=2 -sort=_r -out.max=1 -c="$cowise" | grep -v "^#" > $ft
$stilts tjoin nin=2 in1=$fp ifmt1=votable icmd1='keepcols ppmxl' in2=$ft ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=allwise write=$mode

# AKARI IRC, shift ppmxl coordinates to 2007.0, midway through survey
coirc=`vizquery -mime=votable -source=ppmxl -c.rs=2 -sort=_r -out.max=1 -c="$cojoin" -out="_RA(J2000,2007.0)" | grep "<TR><TD>" | sed s/\<TR\>\<TD\>// | sed s/\<\\\\/TD\>\<TD\>/,/ | sed s/\<\\\\/TD\>\<\\\\/TR\>//`
vizquery -mime=votable -source=II/297 -out.add=_r -c.rs=2 -sort=_r -out.max=1 -c="$coirc" | grep -v "^#" > $ft
$stilts tjoin nin=2 in1=$fp ifmt1=votable icmd1='keepcols ppmxl' in2=$ft ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=akari_irc write=$mode

# SEIP, epoch less certain, roughly 2006.9 for mid-mission so use AKARI
curl -s "http://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query?catalog=slphotdr4&spatial=cone&radius=2&outrows=1&outfmt=3&objstr=$coirc" > $ft
$stilts tjoin nin=2 in1=$fp ifmt1=votable icmd1='keepcols ppmxl' in2=$ft ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$db user=$user password=$pass dbtable=seip write=$mode
