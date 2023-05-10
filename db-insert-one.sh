#!/bin/sh

# get photometry and info on a star from vizier catalogues and put them
# in mysql

# The goal is to arrive at a unique per-star ID based on sexagesimal
# coordinates in J2000 at epoch 2000.0, and get photometry from a
# handful of major catalogues that is matched to this ID. Rows from the
# big tables are downloaded as part of this process, so the script
# checks for existence before going off to vizier. Photometry for a
# given target can then be retrieved simply by grabbing all the rows
# from the various tables that have the desired ID.

# Other photometry, such as Spitzer/Herschel/etc.  where other
# identifiers, such as HD/HIP/2MASS/etc. numbers are given instead of
# coordinates, can be matched using a cross-id table. This is generated
# by querying simbad

# A major future proofing issue is the ID, which could change if the
# position or proper motion of a target change (i.e. going from
# PPMXL->Gaia). Anything derived from this ID, such as entries in other
# tables and file names, will then also need to change. Partial
# resolutions are to limit the precision of the ID (e.g. 0.1arcsec) and
# to use bright-star catalogues (HIP,TYC,UCAC4,PPMXL) in preference to
# Gaia. A more probable solution is to rebuild everything for each Gaia
# release, if that looks necessary. Some version control could be
# incorporated into the ID (e.g. sdb-v1-XXXXXX) to help avoid later
# confusion if duplicate info makes it into the wild.

# script takes name as first arg, which will be looked for with
# sesame. this can be a set of coordinates in 2MASS-style. if two args
# are given these are assumed to coordinates in degrees at epoch
# 2000.0. these will first be used to look for an object name and a more
# refined set of coordinates, failure meaning that there won't be a list
# of cross-ids and the given coords will be used as given

# TODO: hone shell ninja skills to remove all echo/sed rubbish

# some filenames, include randomness in case we want to run this script
# in parallel
fp=/tmp/pos$RANDOM.xml
ft=/tmp/tmp$RANDOM.xml
ft2=/tmp/tmp$RANDOM.xml

# config
source sdb_insert_config
mode=append

echo "/~~~~~ db-insert-one.sh ~~~~~~/"
echo "Using default match radius of $rad arcsec"
echo "and prefix for sdb ids as $sdbprefix"
echo "and $site mirror for vizquery calls"

# if two arguments were given (i.e. coords) try to find a name from
# simbad, failure results in variable "id" being empty
id=$1
if [ $# -eq 2 ]
then
    echo "\nLooking for something at coords: $1 $2"
    id=`curl -s "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20top%201%20main_id%20FROM%20basic%20JOIN%20ident%20ON%20oid%20=%20oidref%20WHERE%20CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',$1,$2,$rad/3600.))=1%20AND%20ra%20IS%20NOT%20NULL%20AND%20dec%20IS%20NOT%20NULL;"  | $stilts tpipe in=- ifmt=votable cmd='random' cmd='keepcols main_id' omode=out out=- ofmt=csv-noheader`
    if [ "$id" != "" ]
    then
        echo "Found id:$id at given coords $1 $2"
    else
        echo "No object found at $1 $2"
    fi
fi

# if given id looks like a coordinate, keep it in case we need to use it
if [[ $1 =~ J[0-9]{6,}[+-][0-9]{6,} ]]
then
    id_coord=$BASH_REMATCH
fi

# if one argument was given (i.e. a name) get ra,dec from sesame, if
# given coords in a 2MASS-like format (need at least 9 digits total)
# then also proceeed. these are corrected to epoch 2000.0 (where
# possible). also attempt to get proper motion
pmra=0.0
pmde=0.0
if [ "$id" != "" ]
then
    echo "\nsesame using name:$id"
    co=`sesame -oIx -rSV "$id" | egrep -w 'jradeg|jdedeg'`
    cojoin=${co//[$'\n']/,}
    cojoin=${cojoin//[^0-9+\-,\.]/}
    # cojoin may contain two sets of jradeg,jdedeg, so take number before
    # first comma for ra and after last for dec 
    ra=`echo $cojoin | sed 's/\([0-9\.]*\),.*/\1/'`
    de=`echo $cojoin | sed 's/.*,\(.*\)/\1/'`
    cojoin=$ra,$de

    echo "  trying sesame for proper motion"
    co=`sesame "$id"`
    ok=`echo $co | grep 'pmRA'`
    if [ "$ok" != "" ]
    then
        pmra=`echo $co | sed 's/.*<pmRA>\(.*\)<\/pmRA>.*/\1/'`
        pmde=`echo $co | sed 's/.*<pmDE>\(.*\)<\/pmDE>.*/\1/'`
        echo "    found pm $pmra,$pmde with sesame"
    fi

    # if this failed, try id_coord from above
    if [ "$cojoin" == "" -o "$cojoin" == "," ]
    then
        if [ "$id_coord" != "" ]
        then
            echo "\nsesame using coord:$id_coord"
            co=`sesame -rSV "$id_coord" | egrep -w 'jradeg|jdedeg'`
            cojoin=${co//[$'\n']/,}
            cojoin=${cojoin//[^0-9+\-,\.]/}
            # extra cut for cases with two+ lots of ra/dec
            cojoin=`echo $cojoin | cut -f1,2 -d','`
            ra=`echo $cojoin | sed 's/\(.*\),.*/\1/'`
            de=`echo $cojoin | sed 's/.*,\(.*\)/\1/'`

            echo "  trying sesame for proper motion"
            co=`sesame "$id_coord"`
            ok=`echo $co | grep 'pmRA'`
            if [ "$ok" != "" ]
            then
                pmra=`echo $co | sed 's/.*<pmRA>\(.*\)<\/pmRA>.*/\1/'`
                pmde=`echo $co | sed 's/.*<pmDE>\(.*\)<\/pmDE>.*/\1/'`
                echo "    found pm $pmra,$pmde with sesame"
            fi

        fi
        # if this didn't work then give up, s
        if [ "$cojoin" == "" -o "$cojoin" == "," ]
        then
            echo "  sesame found nothing for:$1"
            echo "  only id given so nothing to do, exiting"
            exit
        fi
    fi
    echo "  sesame got coords:$cojoin"
else
    echo "  no name found, using given coords"
    cojoin=$1,$2
    ra=$1
    de=$2
fi
echo "\nFinal set of coords:$cojoin"

# if no proper motion was found,
# try to find something at these coords in a table with proper
# motions, this will allow use of epoch-corrected coords when searching
# for matches in other tables below. put in a file to use again below.

# vizquery returns positions as expected, but _r is for original catalogue
# position, so doesn't take proper motion into account!

# sequential trawl through DR2, HIP, PPMXL
if [ $pmra == 0.0 ]
then
    echo "\nLooking in proper motion catalogues"

    # set epoch to 2000.0 by default, but use 1993.3 if a 2MASS id
    epoch=2000.0
    # use an increased radius to capture high pm objects
    rad=5
    if [[ $1 =~ ^2MASS ]]
    then
        epoch=1993.3
        echo "  set epoch to $epoch for 2MASS ID"
    fi

    echo "  Gaia DR2"
    vizquery -site=$site -mime=votable -source=I/345/gaia2 -c.rs=$rad pmRA="!=" -out.max=3 -out.add=_r -c=$cojoin -out="_RA(J2000,$epoch)" -out="_DE(J2000,$epoch)" -out="*pos.pm;pos.eq.ra" -out="*pos.pm;pos.eq.dec" > $ft2
    numrow=`$stilts tpipe in=$ft2 cmd='keepcols _r' cmd='stats NGood' ofmt=csv-noheader`
    if [ "$numrow" == "" ]
    then
        echo "  not in DR2, try HIP"
        vizquery -site=$site -mime=votable -source=I/311/hip2 -c.rs=$rad pmRA="!=" -out.max=3 -out.add=_r -c=$cojoin -out="_RA(J2000,$epoch)" -out="_DE(J2000,$epoch)" -out="*pos.pm;pos.eq.ra" -out="*pos.pm;pos.eq.dec" > $ft2
        numrow=`$stilts tpipe in=$ft2 cmd='keepcols _r' cmd='stats NGood' ofmt=csv-noheader`
        # remove "hidden" Sn column
        $stilts tcat in=$ft2 omode=out ofmt=votable ocmd='delcols "Sn"' out=$ft2
        if [ "$numrow" == "" ]
        then
            echo "  not in DR2, HIP, try PPMXL"
            vizquery -site=$site -mime=votable -source=ppmxl -c.rs=$rad pmRA="!=" -out.max=3 -out.add=_r -c=$cojoin -out="_RA(J2000,$epoch)" -out="_DE(J2000,$epoch)" -out="*pos.pm;pos.eq.ra" -out="*pos.pm;pos.eq.dec" > $ft2
            numrow=`$stilts tpipe in=$ft2 cmd='keepcols _r' cmd='stats NGood' ofmt=csv-noheader`
            if [ "$numrow" == "" ]
            then
                echo "    no PM in Gaia DR2, HIP, ppmxl"
            else
                echo "    ppmxl success"
            fi
        else
            echo "    HIP success"
        fi
    else
        echo "    DR2 success"
    fi

    # get closest match with input coords and overwrite $ft2
    echo "raj2000,dej2000" > $ft
    echo "$cojoin" >> $ft
    $stilts tmatch2 in1=$ft ifmt1=csv in2=$ft2 ifmt2=votable ocmd='keepcols "_r _RAJ2000 _DEJ2000 pmRA pmDE"' matcher=sky values1='raj2000 dej2000' values2='_RAJ2000 _DEJ2000' params='10' find=best omode=out out=$ft2 ofmt=votable > /dev/null

    # update coordinates if a pm was found, an error will be thrown by stilts
    # if the file $ft2 wasn't filled above
    if [ "$numrow" != "" ]
    then
        $stilts tcat in=$ft2 multi=true omode=out ofmt=votable out=$fp
        cojoin=`$stilts tpipe in=$fp ifmt=votable cmd='random' cmd="sort _r" cmd='keepcols "_RAJ2000 _DEJ2000"' cmd="rowrange 1 1" omode=out out=- ofmt=csv-noheader`
        echo "  pm success, updating coords to $cojoin"
    else
        echo "  no pm source found, keeping:$cojoin and assuming pm is zero"
        echo "_r,_raj2000,_dej2000,pmRA,pmDE" > $ft
        echo "-1.0,$cojoin,0.0,0.0" >> $ft
        $stilts tpipe in=$ft ifmt=csv cmd='random' omode=out out=- ofmt=votable > $fp
    fi
else
    # if we got a proper motion from sesame above, keep it
    echo "_r,_raj2000,_dej2000,pmRA,pmDE" > $ft
    echo "-1.0,$cojoin,$pmra,$pmde" >> $ft
    $stilts tpipe in=$ft ifmt=csv cmd='random' omode=out out=- ofmt=votable > $fp
fi

#if [ "$cotmp" != "" ]
#then
#    cojoin=$cotmp
#    echo "  success, updated epoch 2000.0 coord from pm:$cojoin"
#else
#    echo "  trying sesame for proper motion"
#    co=`sesame "$id"`
#    ok=`echo $co | grep 'pmRA'`
#    if [ "$ok" != "" ]
#    then
#        pmra=`echo $co | sed 's/.*<pmRA>\(.*\)<\/pmRA>.*/\1/'`
#        pmde=`echo $co | sed 's/.*<pmDE>\(.*\)<\/pmDE>.*/\1/'`
#        posref=`echo $co | sed 's/.*<refPos>\(.*\)<\/refPos>.*/\1/'`
#        echo "    found pm $pmra,$pmde with sesame, position ref $posref"
#        # attempt to be smart about the position ref and implied epoch
#        if [ "$posref" != "" ]
#        then
#            # ALLWISE papers, position about 2010.3
#            if [ "$posref" == "2012yCat.2311....0C" -o "$posref" == "2011ApJ...726...30M" -o "$posref" == "2011ApJS..197...19K" -o "$posref" == "2014ApJ...786L..18L" ]
#            then
#                 ra=`echo "$ra-10.3*$pmra/1000.0/3600.0/c($de*a(1)/45.0)" | bc -l`
#                 de=`echo "$de-10.3*$pmde/1000.0/3600.0" | bc -l`
#            fi
#            cojoin=$ra,$de
#            echo "      ep2000.0 coord changed:$cojoin"
#        else
#            echo "      ep2000.0 coord the same:$cojoin"
#        fi
#    else
#        echo "  no pm source found, keeping:$cojoin and assuming pm is zero"
#        pmra=0.0
#        pmde=0.0
#    fi
#
#    echo "_r,_raj2000,_dej2000,pmRA,pmDE" > $ft
#    echo "-1.0,$cojoin,$pmra,$pmde" >> $ft
#    $stilts tpipe in=$ft ifmt=csv cmd='random' omode=out out=- ofmt=votable > $fp
#fi

# convert these coords to an ID to be used for this target, need to put
# coords in a temporary file first since stilts can't stream ascii
echo "\nCreating sdb id"
echo ra,dec > $ft
echo $cojoin >> $ft
sdbid=$sdbprefix`$stilts tpipe in=$ft ifmt=csv cmd='random' cmd='replacecol ra degreesToHms(ra,2)' cmd='replacecol dec degreesToDms(dec,1)' omode=out out=- ofmt=csv-noheader | sed "s/://g" | sed "s/,//"`
echo "  source id is:$sdbid"

# check there isn't an existing target with a slightly different coord
dup=$(mysql $db -N -e "SELECT sdbid FROM sdb_pm WHERE sdbid != '$sdbid' AND raj2000 BETWEEN $ra-0.0001 AND $ra+0.0001 AND dej2000 BETWEEN $de-0.0001 AND $de+0.0001;")
if [ "$dup" != "" ]
then
    echo "\nPossible duplicate source found nearby"
#    echo "\nDuplicate source $dup found nearby, deleting before proceeding"
#    ./db-delete-one.sh $dup
fi

# finally, see if we have this sbdid already
res=$(mysql $db -N -e "SELECT sdbid FROM xids WHERE BINARY xid='$sdbid';")
if [[ $res = $sdbid ]]
then
    echo "\nStopping here, have sdbid $sdbid in xids table"
    echo "------- db-insert-one.sh -------"
    exit
else
    echo "\nNew target, going ahead"
fi

# place an entry that we'll remove on completion
if [ $# -eq 2 ]
then
    mysql $db -N -e "INSERT INTO import_failed (sdbid,arg1,arg2) VALUES ('$sdbid','$1','$2');"
else
    mysql $db -N -e "INSERT INTO import_failed (sdbid,arg1) VALUES ('$sdbid','$1');"
fi

# clear any previous xids
mysql $db -e "DELETE FROM xids WHERE sdbid='$sdbid';"

# create temp file with sdbid in it, will use multiple times below
# including being added as a xid. using an ascii table here works better
# since the header isn't recognised when in csv
echo "#sdbid                    xid" > $ft
#     sdb-v1-183656.34+374701.3 bla  # to get spacing right for ascii table
echo $sdbid $sdbid >> $ft

# keep closest row with proper motions for later, add sdbid and rename
# columns to be clearer, grab this back out and update the $fp file
echo "\nWriting proper motion info to db"
$stilts tjoin nin=2 in1=$ft ifmt1=ascii icmd1="keepcols sdbid" in2=$fp ifmt2=votable icmd2="rowrange 1 1" ocmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=sdb_pm write=$mode
$stilts sqlclient db='jdbc:mysql://localhost/'$sdb user=$user password=$password sql="SELECT * from sdb_pm where sdbid = '$sdbid'" ofmt=votable > $fp

# if we had success with sesame above, use this name to get a list of
# crossids from simbad and some other info, sdbid is the main id
if [ "$id" != "" ]
then
    echo "\nUsing id $id to find xids"
    # xids, remove duplicates that just have different case, sql setup is case-insensitive
    cid=`echo "$id" | sed 's/ /%20/g' | sed 's/+/%2B/g' | sed 's/\*/%2A/g' | sed 's/\[/%5B/g' | sed 's/\]/%5D/g'`
    csdbid=`echo "$sdbid" | sed 's/+/%2B/g'`
    curl -s "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20%27$csdbid%27%20as%20sdbid,id2.id%20as%20xid%20FROM%20ident%20AS%20id1%20JOIN%20ident%20AS%20id2%20USING(oidref)%20WHERE%20id1.id=%27$cid%27;" | $stilts tpipe in=- ifmt=votable cmd='random' cmd='addcol xiduc toUpperCase(xid)' cmd='sort xiduc' cmd='uniq xiduc' cmd='delcols xiduc' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=xids write=$mode

    # simbad
    echo "\nUsing id $id to find simbad info"
    curl -s "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20basic.main_id,sp_type,sp_bibcode,plx_value,plx_err,plx_bibcode,otype_shortname,otype_longname%20FROM%20basic%20JOIN%20ident%20ON%20ident.oidref%20=%20oid%20JOIN%20otypedef%20on%20basic.otype%20=%20otypedef.otype%20WHERE%20id=%27$cid%27" > $ft2
    $stilts tjoin nin=2 in1=$ft ifmt1=ascii icmd1='keepcols sdbid' in2=$ft2 ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=simbad write=$mode
fi

# sanity check, that no xid matches the given id for another sdbid
res=$(mysql $db -N -e "SELECT sdbid,xid FROM xids WHERE BINARY xid='$id' and sdbid != '$sdbid';")
if [ "$res" != "" ]
then
    echo "\nERROR: Found xid for $id different to $sdbid: $res"
    exit
fi

# remove any double spaces in the xids
mysql $db -N -e "UPDATE xids SET xid = REPLACE(REPLACE(REPLACE(xid,' ','<>'),'><',''),'<>',' ') WHERE sdbid='$sdbid';"

# add given id as a xid. this is a potential issue if the given id is
# not unique. by leaving this option on we assume we'll only ever be
# given unique ids. this should be OK as an id will either be a
# coordinate and so must be fairly precise, or will be successfully
# resolved by sesame, and hence unambiguous.
if [ "$id" != "" -a 1 == 1 ]
then
    # check we don't have it as an xid already
    res=$(mysql $db -N -e "SELECT xid FROM xids WHERE BINARY xid='$id';")
    if [ "$res" == "" ]
    then
        echo "\nAdding given id as an xid"
        echo $sdbid \"$id\" >> $ft
    else
        echo "\nNot adding given id as xid, already in list"
    fi
fi
echo "\nUpdating xids to include sbdid"
$stilts tpipe in=$ft ifmt=ascii cmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=xids write=append

# collect some more crossids that may not exist already, this is used
# for tables that exist in full in the databse already

# IRAS point source catalogues
./sdb_iras_xids.sh $sdbid

# Herschel point source catalogues
for t in HPPSC_070_v1 HPPSC_100_v1 HPPSC_160_v1 HPESL_v1 spsc_standard_250_v2 spsc_standard_350_v2 spsc_standard_500_v2;
do
    ./sdb_herschel_psc_xids.sh "$sdbid" "$t"
done

#### now do catalogues we're not going to store in their entirety but
#### download as we need. these are sorted roughly in wavelength order

# GALEX
./sdb_insert_galex.sh $sdbid

# Tycho-2
./sdb_insert_tyc2.sh $sdbid

# APASS
./sdb_insert_apass.sh $sdbid

# SDSS
./sdb_insert_sdss12.sh $sdbid

# Gaia
#./sdb_insert_gaia.sh $sdbid
./sdb_insert_gaia_dr2.sh $sdbid

# DENIS
./sdb_insert_denis.sh $sdbid

# 2MASS
./sdb_insert_2mass.sh $sdbid

# ALLWISE
./sdb_insert_allwise.sh $sdbid

# catWISE
./sdb_insert_catwise.sh $sdbid

# AKARI IRC
./sdb_insert_akari_irc.sh $sdbid

# SEIP
./sdb_insert_seip.sh $sdbid

# IRS spectra
./sdb_insert_irsstare.sh $sdbid

# clean up
rm $fp
rm $ft
rm $ft2

# note success (or at least completion)
mysql $db -N -e "DELETE FROM import_failed WHERE sdbid = '$sdbid';"

echo "\nDone $sdbid"
echo "------- db-insert-one.sh -------"
