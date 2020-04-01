/* create alma-observed sample, execute from sdb db */
create table sdb_samples.alma_obs select distinct main_id from sdb_pm left join simbad using (sdbid) left join alma_obslog on raj2000 between ra-10/3600 and ra+10/3600 and dej2000 between dec_-10/3600 and dec_+10/3600 where project_code is not null;
