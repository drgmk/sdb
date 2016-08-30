/* KEYS - run this after recreating all tables with dropcreate, make sure this is done
   with something found for all tables to get field lengths correct (hd181327). then change
   to append and run once without exiting for duplicates, then uncomment and go ahead... */

ALTER TABLE sdb_pm ADD PRIMARY KEY (sdbid);

ALTER TABLE xids ADD PRIMARY KEY (sdbid,xid);
ALTER TABLE xids ADD FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
ALTER TABLE xids ADD UNIQUE (xid);
ALTER TABLE xids MODIFY xid VARCHAR(100);

CREATE UNIQUE INDEX sdbid_2m ON 2mass (sdbid);
ALTER TABLE 2mass ADD CONSTRAINT sdbid_2m FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
CREATE UNIQUE INDEX sdbid_ak ON akari_irc (sdbid);
ALTER TABLE akari_irc ADD CONSTRAINT sdbid_ak FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
CREATE UNIQUE INDEX sdbid_aw ON allwise (sdbid);
ALTER TABLE allwise ADD CONSTRAINT sdbid_aw FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
CREATE UNIQUE INDEX sdbid_sp ON seip (sdbid);
ALTER TABLE seip ADD CONSTRAINT sdbid_sp FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
CREATE UNIQUE INDEX sdbid_ty ON tyc2 (sdbid);
ALTER TABLE tyc2 ADD CONSTRAINT sdbid_ty FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
CREATE UNIQUE INDEX sdbid_sm ON simbad (sdbid);
ALTER TABLE simbad ADD CONSTRAINT sdbid_sm FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
ALTER TABLE simbad MODIFY main_id VARCHAR(100);
ALTER TABLE simbad MODIFY sp_type VARCHAR(36);
ALTER TABLE simbad MODIFY sp_bibcode VARCHAR(19);
ALTER TABLE simbad MODIFY plx_bibcode VARCHAR(19);

/* remove extra whitespace on names put in xids */
CREATE TRIGGER xid_remove_whitespace BEFORE INSERT ON `sed_db`.xids FOR EACH ROW SET NEW.xid=trim(replace(replace(replace(replace(replace(NEW.xid,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' '));
/* same for names in simbad table */
CREATE TRIGGER simbad_remove_whitespace BEFORE INSERT ON `sed_db`.simbad FOR EACH ROW SET NEW.main_id=trim(replace(replace(replace(replace(replace(NEW.main_id,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' '));


/* CODE */

/* cleanup to start from scratch */
DROP TABLE IF EXISTS 2mass;
DROP TABLE IF EXISTS akari_irc;
DROP TABLE IF EXISTS allwise;
DROP TABLE IF EXISTS seip;
DROP TABLE IF EXISTS simbad;
DROP TABLE IF EXISTS tyc2;
DROP TABLE IF EXISTS xids;
DROP TABLE IF EXISTS sdb_pm;

/* add samples to projects table */
alter table sample_shardds add column sdbid bigint(19);
update sample_shardds left join xids on name=xid set sample_shardds.sdbid=xids.sdbid;
ALTER TABLE sample_shardds ADD FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

delete from projects where project='shardds';
insert into projects select sdbid,'shardds' from sample_shardds where sbdid is not null;

/* sed fitting tables */
CREATE TABLE `sed_totflux` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(30) DEFAULT NULL,
  `band` varchar(10) DEFAULT NULL,
  `flux` float DEFAULT NULL,
  `cc_atoq` float DEFAULT NULL,
  `error` float DEFAULT NULL,
  `obs` float DEFAULT NULL,
  `e_obs` float DEFAULT NULL,
  `obs_uplim` int(11) DEFAULT NULL,
  `obs_ref` varchar(20) DEFAULT NULL,
  `R` float DEFAULT NULL,
  `chi` float DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=MyISAM AUTO_INCREMENT=1798017 DEFAULT CHARSET=latin1;

CREATE TABLE `sed_bbfit` (
  `bbfit_lastupdate` double DEFAULT NULL,
  `name` varchar(30) NOT NULL DEFAULT '',
  `disksig` float DEFAULT NULL,
  `tdisk_hot` float DEFAULT NULL,
  `e_tdisk_hot` float DEFAULT NULL,
  `bbnmlz_hot` float DEFAULT NULL,
  `e_bbnmlz_hot` float DEFAULT NULL,
  `tdisk_cold` float DEFAULT NULL,
  `e_tdisk_cold` float DEFAULT NULL,
  `tdisk_cold_constr_lo` int(11) DEFAULT NULL,
  `tdisk_cold_constr_hi` int(11) DEFAULT NULL,
  `bbnmlz_cold` float DEFAULT NULL,
  `e_bbnmlz_cold` float DEFAULT NULL,
  `lambda0` float DEFAULT NULL,
  `e_lambda0` float DEFAULT NULL,
  `lambda0_constr_lo` int(11) DEFAULT NULL,
  `lambda0_constr_hi` int(11) DEFAULT NULL,
  `beta` float DEFAULT NULL,
  `e_beta` float DEFAULT NULL,
  `beta_constr_lo` int(11) DEFAULT NULL,
  `beta_constr_hi` int(11) DEFAULT NULL,
  `bb_chisqdof` float DEFAULT NULL,
  `rdisk_cold` float DEFAULT NULL,
  `e_rdisk_cold` float DEFAULT NULL,
  `diamdisk_cold_arcs` float DEFAULT NULL,
  `e_diamdisk_cold` float DEFAULT NULL,
  `ldisklstar_hot` float DEFAULT NULL,
  `ldisklstar_cold` float DEFAULT NULL,
  `ldisk` float DEFAULT NULL,
  `e_ldisk` float DEFAULT NULL,
  `ldisklstar` float DEFAULT NULL,
  `e_ldisklstar` float DEFAULT NULL,
  `sigtot_AU2` float DEFAULT NULL,
  `e_sigtot` float DEFAULT NULL,
  `mdisk_ME` float DEFAULT NULL,
  `e_mdisk` float DEFAULT NULL,
  PRIMARY KEY (`name`)
) ENGINE=MyISAM DEFAULT CHARSET=latin1;

CREATE TABLE `sed_photosphere` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(30) DEFAULT NULL,
  `band` varchar(10) DEFAULT NULL,
  `flux` float DEFAULT NULL,
  `cc_atoq` float DEFAULT NULL,
  `error` float DEFAULT NULL,
  `obs` float DEFAULT NULL,
  `e_obs` float DEFAULT NULL,
  `obs_uplim` int(11) DEFAULT NULL,
  `obs_ref` varchar(20) DEFAULT NULL,
  `R` float DEFAULT NULL,
  `chi` float DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=MyISAM AUTO_INCREMENT=1798273 DEFAULT CHARSET=latin1;

CREATE TABLE `sed_stfit` (
  `stfit_lastupdate` double DEFAULT NULL,
  `name` varchar(30) NOT NULL DEFAULT '',
  `teff` float DEFAULT NULL,
  `e_teff` float DEFAULT NULL,
  `nmlz` float DEFAULT NULL,
  `e_nmlz` float DEFAULT NULL,
  `rstar` float DEFAULT NULL,
  `e_rstar` float DEFAULT NULL,
  `lstar` float DEFAULT NULL,
  `e_lstar` float DEFAULT NULL,
  `logg` float DEFAULT NULL,
  `e_logg` float DEFAULT NULL,
  `mh` float DEFAULT NULL,
  `e_mh` float DEFAULT NULL,
  `av` float DEFAULT NULL,
  `e_av` float DEFAULT NULL,
  `dist` float DEFAULT NULL,
  `sed_chisqdof` float DEFAULT NULL,
  PRIMARY KEY (`name`)
) ENGINE=MyISAM DEFAULT CHARSET=latin1;
