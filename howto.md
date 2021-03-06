#sdb

A database of stars, primarily focussed on SEDs and IR excesses, but 
probably useful for other things too. Structure is based around samples,
which might be for example surveys, publications, or physical 
associations. Aggregate information for samples is available in plots.

##Initial setup

* Use backup/initial-sdb.sql to initialise database

   This includes all the tables used by db-insert-one.sh (these are actually created by
   this script), but some have been modified to ensure the columns have the right formats
   (i.e. varchar(xx) is long enough). Primary keys are also all set up. All this could be
   reproduced from scratch by running db-insert-one.sh with the "dropcreate" option set,
   which will create the tables anew. Then sql.sql has some code to set up the keys and a
   few column formats. Some more for the seip table can be deduced from the formats given
   for this table on irsa.

* Open clean-www.tgz for a fresh www structure if necessary

   The structure is very simple, using .htaccess, .htgroup, and .htpasswd files to
   restrict access. Directories are created for each sample, and individual SED
   directories are also protected where they have proprietary photometry. Navigation is
   (currently) via apache's directory listing.

##Catalogues

For now this is only the sub-mm compilation. Is extracted from the database with:

/Applications/stilts -classpath /Library/Java/Extensions/mysql-connector-java-5.1.8-bin.jar -Djdbc.drivers=com.mysql.jdbc.Driver sqlclient db='jdbc:mysql://localhost/debris' user=X password=X sql="SELECT * FROM submm_obs ORDER BY ref DESC" ofmt=ipac

and put back with:

tosql.pl -d X -f submm_obs.txt -i ipac

##Standard workflow

Further instructions at each step are given in the relevant codes.

1. Add targets with db-insert-one.sh or db-insert-many.sh
--* add sdbid column to sample and sample to projects table if necessary
2. Grab spectra files, CASSIS for now with cassis_download.sh
3. Extract photometry into *-rawphot files with sdb_getphot.py
4. Do fitting with sdf's sdf-fit
5. Generate tables and plots with sdf-sample

##TODOs

There are a great many of these, way more than listed here.

- [ ] Query for specific system
- [ ] Separate SED results by sample (currently just by sdbid)
- [ ] Way to remove obsolete SED directories when photometry become public
- [ ] www-queryable basic parameters for each system
- [ ] Daily list of results for targets that appeared in astro-ph
- [ ] Include as many tables of photometry as possible, particularly far-IR/mm ones
- [ ] Complete list of IRS spectra, currently an auto download that misses things (e.g. ptg=1)
- [ ] Way for user to see which SEDs are available to them
- [ ] Better SED fitting software, current is very good, but disk models basic/crappy
- [ ] Management of SED fitting configuration, so far global (i.e. non-existent)
- [ ] Automation of as much as possible
- [ ] Pretty interface
