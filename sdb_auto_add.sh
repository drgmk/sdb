#!/bin/sh

# TODO: fix all this up so it works from startup

# run when files added to auto_add dir, this is launched with a launchd plist by
# launchctl load ~/Library/LaunchAgents/sdb_auto_watch.plist, which watches the auto_add
# folder. Upon addition of a file this script is launched

# launchctl needs to have the environment variables set properly, which are:

# launchctl setenv PATH /usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/exelis/idl/bin:/usr/local/mysql/bin

# launchctl setenv IDL_STARTUP /Users/grant/.idlrc

root=/Users/grant/astro/projects/sdb/sdb/
pushd $root
add=$root../auto_add/add.txt

if [ -e $add ]
then

    while read -r line
    do
	id=${line//[^a-zA-Z0-9 ]/}
	echo "Got:""$id"
	./sdb_auto_add_one.sh "$id"
	
    done < $add
    
    rm $add
fi

# osascript -e "tell Application \"System Events\" to display alert \"$id added to sdb\""

popd
