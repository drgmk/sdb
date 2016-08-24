#!/bin/sh

# create symbolic links to master files for a given project
loc=$HOME/astro/projects/sed-db/seds/masters

# some other knobs
db=sed_db
user=grant
pass=grant

# given sample db name in $1
mysql -u$user -p$pass $db -N -e "SELECT ppmxl FROM $1" | while read ppmxl
do
    id=$(mysql -u$user -p$pass $db -N -e "SELECT name FROM $1 WHERE ppmxl=$ppmxl";)

    echo linking : $ppmxl to $f2
    
    # SEDs
    ls $loc/$ppmxl/$ppmxl.png | sed 's/.*\///' | while read img
    do
	f1=`echo $img | sed s/$ppmxl/"$id"/`
	f2=`echo "$f1" | sed 's/ /-/g'`
	ln -s $loc/$ppmxl/$img $f2
    done

    # detection limits
    ls $loc/$ppmxl/$ppmxl-detlim.png | sed 's/.*\///' | while read img
    do
	f1=`echo $img | sed s/$ppmxl/"$id"/`
	f2=`echo "$f1" | sed 's/ /-/g'`
	ln -s $loc/$ppmxl/$img $f2
    done
done
