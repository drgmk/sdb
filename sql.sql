/* TRIGGERS */

/* remove extra whitespace on names put in xids */
drop trigger xid_remove_whitespace;
CREATE TRIGGER xid_remove_whitespace BEFORE INSERT ON `sed_db`.xids FOR EACH ROW SET NEW.xid=trim(replace(replace(replace(replace(replace(NEW.xid,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' '));

/* same for names in simbad table */
drop trigger simbad_remove_whitespace;
CREATE TRIGGER simbad_remove_whitespace BEFORE INSERT ON `sed_db`.simbad FOR EACH ROW SET NEW.main_id=trim(replace(replace(replace(replace(replace(NEW.main_id,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' '));

/* CODE */

/* add samples to projects table */
alter table sample_shardds add column ppmxl bigint(19);
update sample_shardds left join xids on name=xid set sample_shardds.ppmxl=xids.ppmxl;
delete from projects where project='shardds';
insert into projects select ppmxl,'shardds' from sample_shardds where ppmxl is not null;

/* cleanup to start from scratch */
truncate table ppmxl;
truncate table 2mass;
truncate table akari_irc;
truncate table allwise;
truncate table seip;
truncate table simbad;
truncate table xids;
truncate table tyc2;
truncate table bbfit;
truncate table stfit;
truncate table photosphere;
truncate table totflux;

/* sed fitting tables */
CREATE TABLE `totflux` (
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

CREATE TABLE `bbfit` (
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

CREATE TABLE `photosphere` (
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

CREATE TABLE `stfit` (
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
