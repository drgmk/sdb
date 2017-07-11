# ************************************************************
# Sequel Pro SQL dump
# Version 4541
#
# http://www.sequelpro.com/
# https://github.com/sequelpro/sequelpro
#
# Host: localhost (MySQL 5.7.10)
# Database: sdb
# Generation Time: 2017-07-11 07:33:09 +0000
# ************************************************************


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;


# Dump of table 2mass
# ------------------------------------------------------------

DROP TABLE IF EXISTS `2mass`;

CREATE TABLE `2mass` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `raj2000` double DEFAULT NULL,
  `dej2000` double DEFAULT NULL,
  `2mass` varchar(16) DEFAULT NULL,
  `jmag` float DEFAULT NULL,
  `e_jmag` float DEFAULT NULL,
  `hmag` float DEFAULT NULL,
  `e_hmag` float DEFAULT NULL,
  `kmag` float DEFAULT NULL,
  `e_kmag` float DEFAULT NULL,
  `qflg` varchar(3) DEFAULT NULL,
  `rflg` varchar(3) DEFAULT NULL,
  `bflg` varchar(3) DEFAULT NULL,
  `cflg` varchar(3) DEFAULT NULL,
  `xflg` smallint(6) DEFAULT NULL,
  `aflg` smallint(6) DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_2m` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table akari_irc
# ------------------------------------------------------------

DROP TABLE IF EXISTS `akari_irc`;

CREATE TABLE `akari_irc` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `objname` varchar(14) DEFAULT NULL,
  `s09` float DEFAULT NULL,
  `e_s09` float DEFAULT NULL,
  `q_s09` smallint(6) DEFAULT NULL,
  `s18` float DEFAULT NULL,
  `e_s18` float DEFAULT NULL,
  `q_s18` smallint(6) DEFAULT NULL,
  `nd09` smallint(6) DEFAULT NULL,
  `nd18` smallint(6) DEFAULT NULL,
  `x09` smallint(6) DEFAULT NULL,
  `x18` smallint(6) DEFAULT NULL,
  `raj2000` double DEFAULT NULL,
  `dej2000` double DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_ak` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table allwise
# ------------------------------------------------------------

DROP TABLE IF EXISTS `allwise`;

CREATE TABLE `allwise` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `allwise` varchar(19) DEFAULT NULL,
  `raj2000` double DEFAULT NULL,
  `dej2000` double DEFAULT NULL,
  `im` varchar(2) DEFAULT NULL,
  `w1mag` float DEFAULT NULL,
  `e_w1mag` float DEFAULT NULL,
  `w2mag` float DEFAULT NULL,
  `e_w2mag` float DEFAULT NULL,
  `w3mag` float DEFAULT NULL,
  `e_w3mag` float DEFAULT NULL,
  `w4mag` float DEFAULT NULL,
  `e_w4mag` float DEFAULT NULL,
  `jmag` float DEFAULT NULL,
  `e_jmag` float DEFAULT NULL,
  `hmag` float DEFAULT NULL,
  `e_hmag` float DEFAULT NULL,
  `kmag` float DEFAULT NULL,
  `e_kmag` float DEFAULT NULL,
  `ccf` varchar(4) DEFAULT NULL,
  `ex` smallint(6) DEFAULT NULL,
  `var` varchar(4) DEFAULT NULL,
  `pmra` int(11) DEFAULT NULL,
  `e_pmra` int(11) DEFAULT NULL,
  `pmde` int(11) DEFAULT NULL,
  `e_pmde` int(11) DEFAULT NULL,
  `qph` varchar(4) DEFAULT NULL,
  `d2m` float DEFAULT NULL,
  `2m` varchar(2) DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_aw` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table apass
# ------------------------------------------------------------

DROP TABLE IF EXISTS `apass`;

CREATE TABLE `apass` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `recno` int(11) DEFAULT NULL,
  `raj2000` double DEFAULT NULL,
  `dej2000` double DEFAULT NULL,
  `e_raj2000` float DEFAULT NULL,
  `e_dej2000` float DEFAULT NULL,
  `field` bigint(20) DEFAULT NULL,
  `nobs` smallint(6) DEFAULT NULL,
  `mobs` smallint(6) DEFAULT NULL,
  `b_v` float DEFAULT NULL,
  `e_b_v` float DEFAULT NULL,
  `vmag` float DEFAULT NULL,
  `e_vmag` float DEFAULT NULL,
  `bmag` float DEFAULT NULL,
  `e_bmag` float DEFAULT NULL,
  `g_mag` float DEFAULT NULL,
  `e_g_mag` float DEFAULT NULL,
  `r_mag` float DEFAULT NULL,
  `e_r_mag` float DEFAULT NULL,
  `i_mag` float DEFAULT NULL,
  `e_i_mag` float DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_ap` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table denis
# ------------------------------------------------------------

DROP TABLE IF EXISTS `denis`;

CREATE TABLE `denis` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `q_imag` smallint(6) DEFAULT NULL,
  `q_jmag` smallint(6) DEFAULT NULL,
  `q_kmag` smallint(6) DEFAULT NULL,
  `iflg` smallint(4) DEFAULT NULL,
  `jflg` smallint(4) DEFAULT NULL,
  `kflg` smallint(4) DEFAULT NULL,
  `mult` smallint(6) DEFAULT NULL,
  `strip` int(11) DEFAULT NULL,
  `raj2000` double DEFAULT NULL,
  `dej2000` double DEFAULT NULL,
  `imag` float DEFAULT NULL,
  `e_imag` float DEFAULT NULL,
  `jmag` float DEFAULT NULL,
  `e_jmag` float DEFAULT NULL,
  `kmag` float DEFAULT NULL,
  `e_kmag` float DEFAULT NULL,
  `rmag` float DEFAULT NULL,
  `bmag` float DEFAULT NULL,
  `ipsf` smallint(6) DEFAULT NULL,
  `jpsf` smallint(6) DEFAULT NULL,
  `kpsf` smallint(6) DEFAULT NULL,
  `denis` varchar(16) DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_de` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table gaia
# ------------------------------------------------------------

DROP TABLE IF EXISTS `gaia`;

CREATE TABLE `gaia` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `e_pmra` float DEFAULT NULL,
  `e_pmde` float DEFAULT NULL,
  `epsi` float DEFAULT NULL,
  `sepsi` float DEFAULT NULL,
  `e_plx` float DEFAULT NULL,
  `ra_icrs` double DEFAULT NULL,
  `e_ra_icrs` float DEFAULT NULL,
  `de_icrs` double DEFAULT NULL,
  `e_de_icrs` float DEFAULT NULL,
  `source` bigint(20) DEFAULT NULL,
  `plx` float DEFAULT NULL,
  `pmra` double DEFAULT NULL,
  `pmde` double DEFAULT NULL,
  `radecor` float DEFAULT NULL,
  `dup` smallint(6) DEFAULT NULL,
  `_fg_` float DEFAULT NULL,
  `e__fg_` float DEFAULT NULL,
  `_gmag_` float DEFAULT NULL,
  `var` varchar(13) DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_ga` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table galex
# ------------------------------------------------------------

DROP TABLE IF EXISTS `galex`;

CREATE TABLE `galex` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `objid` varchar(19) DEFAULT NULL,
  `fflux` float DEFAULT NULL,
  `e_fflux` float DEFAULT NULL,
  `nflux` float DEFAULT NULL,
  `e_nflux` float DEFAULT NULL,
  `raj2000` double DEFAULT NULL,
  `dej2000` double DEFAULT NULL,
  `fuv` float DEFAULT NULL,
  `e_fuv` float DEFAULT NULL,
  `nuv` float DEFAULT NULL,
  `e_nuv` float DEFAULT NULL,
  `r_fov` double DEFAULT NULL,
  `b` smallint(6) DEFAULT NULL,
  `fafl` smallint(6) DEFAULT NULL,
  `nafl` smallint(6) DEFAULT NULL,
  `fexf` smallint(6) DEFAULT NULL,
  `nexf` smallint(6) DEFAULT NULL,
  `fr` double DEFAULT NULL,
  `nr` double DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_gal` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table import_failed
# ------------------------------------------------------------

DROP TABLE IF EXISTS `import_failed`;

CREATE TABLE `import_failed` (
  `sdbid` varchar(25) NOT NULL,
  PRIMARY KEY (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table notes
# ------------------------------------------------------------

DROP TABLE IF EXISTS `notes`;

CREATE TABLE `notes` (
  `id` varchar(30) NOT NULL DEFAULT '',
  `text` longtext NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;

LOCK TABLES `notes` WRITE;
/*!40000 ALTER TABLE `notes` DISABLE KEYS */;

INSERT INTO `notes` (`id`, `text`)
VALUES
	('HD 108904','IRS spectra contaminated by red source to NW (AORs 22800384 and 26123776).'),
	('HD 145880','IRS LL spectra probably include flux from nearby source.');

/*!40000 ALTER TABLE `notes` ENABLE KEYS */;
UNLOCK TABLES;


# Dump of table phot_exclude
# ------------------------------------------------------------

DROP TABLE IF EXISTS `phot_exclude`;

CREATE TABLE `phot_exclude` (
  `join_id` varchar(25) NOT NULL DEFAULT '',
  `exclude_band` varchar(10) NOT NULL DEFAULT '',
  `exclude_ref` varchar(30) DEFAULT NULL,
  `note` longtext,
  PRIMARY KEY (`join_id`,`exclude_band`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;

LOCK TABLES `phot_exclude` WRITE;
/*!40000 ALTER TABLE `phot_exclude` DISABLE KEYS */;

INSERT INTO `phot_exclude` (`join_id`, `exclude_band`, `exclude_ref`, `note`)
VALUES
	('HD 110058','MIPS70','2014ApJS..211...25C','Table gives an upper limit, but a detection is clear from SHA.'),
	('HIP 1368','MIPS70','2014ApJS..211...25C','Table gives an upper limit, but a detection is clear from SHA.'),
	('J043555.27+163031.2','WISE3P4','2010AJ....140.1868W',NULL),
	('J043555.27+163031.2','WISE4P6','2010AJ....140.1868W',NULL);

/*!40000 ALTER TABLE `phot_exclude` ENABLE KEYS */;
UNLOCK TABLES;


# Dump of table projects
# ------------------------------------------------------------

DROP TABLE IF EXISTS `projects`;

CREATE TABLE `projects` (
  `sdbid` varchar(25) NOT NULL DEFAULT '0',
  `project` varchar(100) NOT NULL DEFAULT '',
  PRIMARY KEY (`sdbid`,`project`),
  CONSTRAINT `sdbid_pr` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table sdb_pm
# ------------------------------------------------------------

DROP TABLE IF EXISTS `sdb_pm`;

CREATE TABLE `sdb_pm` (
  `sdbid` varchar(25) NOT NULL,
  `_r` double DEFAULT NULL,
  `raj2000` double DEFAULT NULL,
  `dej2000` double DEFAULT NULL,
  `pmra` float DEFAULT NULL,
  `pmde` float DEFAULT NULL,
  PRIMARY KEY (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table seip
# ------------------------------------------------------------

DROP TABLE IF EXISTS `seip`;

CREATE TABLE `seip` (
  `sdbid` varchar(25) NOT NULL,
  `objid` varchar(42) DEFAULT NULL,
  `crowded` int(11) DEFAULT NULL,
  `badbkgmatch` int(11) DEFAULT NULL,
  `ra` double DEFAULT NULL,
  `dec_` double DEFAULT NULL,
  `clon` varchar(12) DEFAULT NULL,
  `clat` varchar(13) DEFAULT NULL,
  `l` double DEFAULT NULL,
  `b` double DEFAULT NULL,
  `nmatches` int(11) DEFAULT NULL,
  `nreject` int(11) DEFAULT NULL,
  `nbands` int(11) DEFAULT NULL,
  `i1_fluxtype` int(11) DEFAULT NULL,
  `i2_fluxtype` int(11) DEFAULT NULL,
  `i3_fluxtype` int(11) DEFAULT NULL,
  `i4_fluxtype` int(11) DEFAULT NULL,
  `m1_fluxtype` int(11) DEFAULT NULL,
  `i1_fluxflag` int(11) DEFAULT NULL,
  `i2_fluxflag` int(11) DEFAULT NULL,
  `i3_fluxflag` int(11) DEFAULT NULL,
  `i4_fluxflag` int(11) DEFAULT NULL,
  `m1_fluxflag` int(11) DEFAULT NULL,
  `i1_softsatflag` int(11) DEFAULT NULL,
  `i2_softsatflag` int(11) DEFAULT NULL,
  `i3_softsatflag` int(11) DEFAULT NULL,
  `i4_softsatflag` int(11) DEFAULT NULL,
  `i1_f_ap1` double DEFAULT NULL,
  `i1_df_ap1` double DEFAULT NULL,
  `i1_f_ap2` double DEFAULT NULL,
  `i1_df_ap2` double DEFAULT NULL,
  `i1_f_ap1_bf` double DEFAULT NULL,
  `i1_df_ap1_bf` double DEFAULT NULL,
  `i1_f_ap2_bf` double DEFAULT NULL,
  `i1_df_ap2_bf` double DEFAULT NULL,
  `i1_f_ap1_3siglim` double DEFAULT NULL,
  `i1_f_ap2_3siglim` double DEFAULT NULL,
  `i2_f_ap1` double DEFAULT NULL,
  `i2_df_ap1` double DEFAULT NULL,
  `i2_f_ap2` double DEFAULT NULL,
  `i2_df_ap2` double DEFAULT NULL,
  `i2_f_ap1_bf` double DEFAULT NULL,
  `i2_df_ap1_bf` double DEFAULT NULL,
  `i2_f_ap2_bf` double DEFAULT NULL,
  `i2_df_ap2_bf` double DEFAULT NULL,
  `i2_f_ap1_3siglim` double DEFAULT NULL,
  `i2_f_ap2_3siglim` double DEFAULT NULL,
  `i3_f_ap1` double DEFAULT NULL,
  `i3_df_ap1` double DEFAULT NULL,
  `i3_f_ap2` double DEFAULT NULL,
  `i3_df_ap2` double DEFAULT NULL,
  `i3_f_ap1_bf` double DEFAULT NULL,
  `i3_df_ap1_bf` double DEFAULT NULL,
  `i3_f_ap2_bf` double DEFAULT NULL,
  `i3_df_ap2_bf` double DEFAULT NULL,
  `i3_f_ap1_3siglim` double DEFAULT NULL,
  `i3_f_ap2_3siglim` double DEFAULT NULL,
  `i4_f_ap1` double DEFAULT NULL,
  `i4_df_ap1` double DEFAULT NULL,
  `i4_f_ap2` double DEFAULT NULL,
  `i4_df_ap2` double DEFAULT NULL,
  `i4_f_ap1_bf` double DEFAULT NULL,
  `i4_df_ap1_bf` double DEFAULT NULL,
  `i4_f_ap2_bf` double DEFAULT NULL,
  `i4_df_ap2_bf` double DEFAULT NULL,
  `i4_f_ap1_3siglim` double DEFAULT NULL,
  `i4_f_ap2_3siglim` double DEFAULT NULL,
  `m1_f_psf` double DEFAULT NULL,
  `m1_df_psf` double DEFAULT NULL,
  `m1_f_ap` double DEFAULT NULL,
  `m1_df_ap` double DEFAULT NULL,
  `m1_f_psf_bf` double DEFAULT NULL,
  `m1_df_psf_bf` double DEFAULT NULL,
  `m1_f_ap_bf` double DEFAULT NULL,
  `m1_df_ap_bf` double DEFAULT NULL,
  `m1_f_psf_3siglim` double DEFAULT NULL,
  `m1_f_ap_3siglim` double DEFAULT NULL,
  `i1_extfrac` double DEFAULT NULL,
  `i2_extfrac` double DEFAULT NULL,
  `i3_extfrac` double DEFAULT NULL,
  `i4_extfrac` double DEFAULT NULL,
  `m1_extfrac` double DEFAULT NULL,
  `i1_brtfrac` double DEFAULT NULL,
  `i2_brtfrac` double DEFAULT NULL,
  `i3_brtfrac` double DEFAULT NULL,
  `i4_brtfrac` double DEFAULT NULL,
  `m1_brtfrac` double DEFAULT NULL,
  `i1_snr` double DEFAULT NULL,
  `i2_snr` double DEFAULT NULL,
  `i3_snr` double DEFAULT NULL,
  `i4_snr` double DEFAULT NULL,
  `m1_snr` double DEFAULT NULL,
  `i1_seflags` int(11) DEFAULT NULL,
  `i2_seflags` int(11) DEFAULT NULL,
  `i3_seflags` int(11) DEFAULT NULL,
  `i4_seflags` int(11) DEFAULT NULL,
  `m1_dflag` varchar(41) DEFAULT NULL,
  `m1_sflag` int(11) DEFAULT NULL,
  `i1_meannoise` double DEFAULT NULL,
  `i2_meannoise` double DEFAULT NULL,
  `i3_meannoise` double DEFAULT NULL,
  `i4_meannoise` double DEFAULT NULL,
  `m1_meannoise` double DEFAULT NULL,
  `smid` varchar(8) DEFAULT NULL,
  `regid` varchar(41) DEFAULT NULL,
  `irac_obstype` int(11) DEFAULT NULL,
  `mips_obstype` int(11) DEFAULT NULL,
  `i1_coverage` double DEFAULT NULL,
  `i2_coverage` double DEFAULT NULL,
  `i3_coverage` double DEFAULT NULL,
  `i4_coverage` double DEFAULT NULL,
  `m1_coverage` double DEFAULT NULL,
  `twomass_key` bigint(19) DEFAULT NULL,
  `twomass_assoc` int(11) DEFAULT NULL,
  `twomass_ra` double DEFAULT NULL,
  `twomass_dec` double DEFAULT NULL,
  `j` double DEFAULT NULL,
  `dj` double DEFAULT NULL,
  `h` double DEFAULT NULL,
  `dh` double DEFAULT NULL,
  `k` double DEFAULT NULL,
  `dk` double DEFAULT NULL,
  `wise_ra` double DEFAULT NULL,
  `wise_dec` double DEFAULT NULL,
  `wise1` double DEFAULT NULL,
  `dwise1` double DEFAULT NULL,
  `wise2` double DEFAULT NULL,
  `dwise2` double DEFAULT NULL,
  `wise3` double DEFAULT NULL,
  `dwise3` double DEFAULT NULL,
  `wise4` double DEFAULT NULL,
  `dwise4` double DEFAULT NULL,
  `wise_cc_flags` varchar(4) DEFAULT NULL,
  `wise_ext_flg` int(11) DEFAULT NULL,
  `wise_var_flg` varchar(4) DEFAULT NULL,
  `wise_ph_qual` varchar(4) DEFAULT NULL,
  `wise_det_bit` int(11) DEFAULT NULL,
  `wise1rchi2` double DEFAULT NULL,
  `wise1m` int(11) DEFAULT NULL,
  `wise1nm` int(11) DEFAULT NULL,
  `wise2rchi2` double DEFAULT NULL,
  `wise2m` int(11) DEFAULT NULL,
  `wise2nm` int(11) DEFAULT NULL,
  `wise3rchi2` double DEFAULT NULL,
  `wise3m` int(11) DEFAULT NULL,
  `wise3nm` int(11) DEFAULT NULL,
  `wise4rchi2` double DEFAULT NULL,
  `wise4m` int(11) DEFAULT NULL,
  `wise4nm` int(11) DEFAULT NULL,
  `dist` double DEFAULT NULL,
  `angle` double DEFAULT NULL,
  `id` varchar(1) DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_sp` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table simbad
# ------------------------------------------------------------

DROP TABLE IF EXISTS `simbad`;

CREATE TABLE `simbad` (
  `sdbid` varchar(25) NOT NULL,
  `main_id` varchar(100) DEFAULT NULL,
  `sp_type` varchar(36) DEFAULT NULL,
  `sp_bibcode` varchar(19) DEFAULT NULL,
  `plx_value` double DEFAULT NULL,
  `plx_err` float DEFAULT NULL,
  `plx_bibcode` varchar(19) DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  CONSTRAINT `sdbid_sm` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;


DELIMITER ;;
/*!50003 SET SESSION SQL_MODE="NO_AUTO_CREATE_USER,NO_ENGINE_SUBSTITUTION" */;;
/*!50003 CREATE */ /*!50017 DEFINER=`grant`@`localhost` */ /*!50003 TRIGGER `simbad_remove_whitespace` BEFORE INSERT ON `simbad` FOR EACH ROW SET NEW.main_id=trim(replace(replace(replace(replace(replace(NEW.main_id,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' ')) */;;
DELIMITER ;
/*!50003 SET SESSION SQL_MODE=@OLD_SQL_MODE */;


# Dump of table spectra
# ------------------------------------------------------------

DROP TABLE IF EXISTS `spectra`;

CREATE TABLE `spectra` (
  `sdbid` varchar(25) NOT NULL DEFAULT '',
  `instrument` varchar(20) NOT NULL DEFAULT '',
  `aor_key` int(11) NOT NULL,
  `bibcode` varchar(19) NOT NULL DEFAULT '',
  `private` int(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`sdbid`,`instrument`,`aor_key`),
  CONSTRAINT `sdbid_spec` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table spectra_exclude
# ------------------------------------------------------------

DROP TABLE IF EXISTS `spectra_exclude`;

CREATE TABLE `spectra_exclude` (
  `aor_key` int(11) NOT NULL,
  `instrument` varchar(20) NOT NULL DEFAULT '',
  `exclude` tinyint(1) NOT NULL,
  PRIMARY KEY (`aor_key`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;

LOCK TABLES `spectra_exclude` WRITE;
/*!40000 ALTER TABLE `spectra_exclude` DISABLE KEYS */;

INSERT INTO `spectra_exclude` (`aor_key`, `instrument`, `exclude`)
VALUES
	(3573248,'irsstare',1),
	(5362944,'irsstare',1),
	(8971264,'irsstare',1),
	(12687104,'irsstare',1),
	(12704512,'irsstare',1),
	(12717568,'irsstare',1),
	(14972672,'irsstare',1),
	(22800384,'irsstare',1),
	(26123776,'irsstare',1);

/*!40000 ALTER TABLE `spectra_exclude` ENABLE KEYS */;
UNLOCK TABLES;


# Dump of table tyc2
# ------------------------------------------------------------

DROP TABLE IF EXISTS `tyc2`;

CREATE TABLE `tyc2` (
  `sdbid` varchar(25) NOT NULL,
  `tyc2id` varchar(11) DEFAULT NULL,
  `_r` double DEFAULT NULL,
  `e_btmag` float DEFAULT NULL,
  `e_vtmag` float DEFAULT NULL,
  `prox` smallint(6) DEFAULT NULL,
  `ccdm` varchar(3) DEFAULT NULL,
  `tyc1` smallint(6) DEFAULT NULL,
  `tyc2` smallint(6) DEFAULT NULL,
  `tyc3` smallint(6) DEFAULT NULL,
  `pmra` float DEFAULT NULL,
  `pmde` float DEFAULT NULL,
  `btmag` float DEFAULT NULL,
  `vtmag` float DEFAULT NULL,
  `hip` int(11) DEFAULT NULL,
  `ra_icrs_` double DEFAULT NULL,
  `de_icrs_` double DEFAULT NULL,
  PRIMARY KEY (`sdbid`),
  UNIQUE KEY `tyc2id` (`tyc2id`),
  KEY `hip` (`hip`),
  CONSTRAINT `sdbid_ty` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;



# Dump of table xids
# ------------------------------------------------------------

DROP TABLE IF EXISTS `xids`;

CREATE TABLE `xids` (
  `sdbid` varchar(25) NOT NULL DEFAULT '',
  `xid` varchar(50) NOT NULL DEFAULT '',
  PRIMARY KEY (`sdbid`,`xid`),
  KEY `sdbid_sdbid` (`sdbid`),
  KEY `xid` (`xid`),
  CONSTRAINT `sdbid_sdbid` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;


DELIMITER ;;
/*!50003 SET SESSION SQL_MODE="NO_AUTO_CREATE_USER,NO_ENGINE_SUBSTITUTION" */;;
/*!50003 CREATE */ /*!50017 DEFINER=`grant`@`localhost` */ /*!50003 TRIGGER `xid_remove_whitespace` BEFORE INSERT ON `xids` FOR EACH ROW SET NEW.xid=trim(replace(replace(replace(replace(replace(NEW.xid,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' ')) */;;
DELIMITER ;
/*!50003 SET SESSION SQL_MODE=@OLD_SQL_MODE */;


# Dump of table xmatch
# ------------------------------------------------------------

DROP TABLE IF EXISTS `xmatch`;

CREATE TABLE `xmatch` (
  `incl` tinyint(1) DEFAULT NULL,
  `table` varchar(100) NOT NULL DEFAULT '',
  `cat_name` varchar(50) DEFAULT NULL,
  `band_name` varchar(10) DEFAULT NULL,
  `xid` varchar(200) DEFAULT NULL,
  `band` varchar(100) NOT NULL DEFAULT '',
  `col` varchar(100) DEFAULT NULL,
  `ecol` varchar(100) DEFAULT NULL,
  `syserr` varchar(100) DEFAULT NULL,
  `lim` varchar(100) DEFAULT NULL,
  `bibcode` varchar(100) DEFAULT NULL,
  `unit` varchar(20) DEFAULT NULL,
  `com1` varchar(200) DEFAULT NULL,
  `com2` varchar(200) DEFAULT NULL,
  `exclude` varchar(200) DEFAULT NULL,
  `exclude_join` varchar(200) DEFAULT NULL,
  `extra` varchar(200) DEFAULT NULL,
  `private` tinyint(1) NOT NULL,
  PRIMARY KEY (`table`,`band`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;

LOCK TABLES `xmatch` WRITE;
/*!40000 ALTER TABLE `xmatch` DISABLE KEYS */;

INSERT INTO `xmatch` (`incl`, `table`, `cat_name`, `band_name`, `xid`, `band`, `col`, `ecol`, `syserr`, `lim`, `bibcode`, `unit`, `com1`, `com2`, `exclude`, `exclude_join`, `extra`, `private`)
VALUES
	(-1,'bei06b',NULL,NULL,'bei06b.Name','\'MIPS24\'','F24','0','F24*0.01','0','\'2006ApJ...652.1674B\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'bry06',NULL,NULL,'HD','\'MIPS24\'','F24','0','F24*0.01','0','\'2006ApJ...636.1098B\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'bryden09',NULL,NULL,'StarName','\'MIPS24\'','F_24','0','F_24*0.01','0','\'2009ApJ...705.1226B\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'carp09_us',NULL,NULL,'carp09_us.Name1','\'MIPS24\'','S24','e_S24','S24*0.01','0','\'2009ApJ...705.1646C\'','mJy','CONCAT(\'AOR:\',IFNULL(AORKEY,\'\'))','CONCAT(\'Name:\',IFNULL(carp09_us.Name,\'\'))',NULL,NULL,NULL,0),
	(-1,'chen14_t2',NULL,NULL,'chen14_t2.ID','\'MIPS24\'','F24','e_F24','F24*0.01','0','\'2014ApJS..211...25C\'','mJy','CONCAT(\'Name:\',IFNULL(chen14_t2.Name,\'\'))','CONCAT(\'r_F24:\',IFNULL(r_F24,\'\'))',NULL,NULL,NULL,0),
	(1,'debris.misc_IR_obs','Literature IR',NULL,'misc_IR_obs.id','band','fnu_mJy','err_mJy','0','3siglim','ref','mJy','IFNULL(misc_IR_obs.name,\'\')','CONCAT(\'ID:\',IFNULL(ID,\'\'))',NULL,NULL,NULL,0),
	(1,'debris.submm_obs','Literature mm',NULL,'submm_obs.id','CONCAT(\'WAV\',wav)','fnu_mJy','err_mJy','0','3siglim','ref','mJy','CONCAT(\'Instr:\',IFNULL(instrument,\'\'))','CONCAT(\'Name:\',IFNULL(name,\'\'))',NULL,NULL,NULL,0),
	(1,'debris.uns_multiple_comp_crossref','Herschel DEBRIS',NULL,'debris.uns_multiple_comp_crossref.sourceid LEFT JOIN debris.psffitting_current ON CONCAT(unsid,comps)=psffitting_current.name','\'PACS100\'','IF(aprad>5,apflux,1.19*flux)','IF(aprad>5,rand_e_aper,1.19*e_flux)','0.057*IF(aprad>5,apflux,flux)','0','\'2018.DEBRIS\'','Jy','CONCAT(\'chisq:\',IFNULL(chisq_dof,\'\'))','CONCAT(\'Phot:\',IF(aprad>5,\'Ap\',\'PSF\'))',NULL,NULL,' AND wav=100 AND name NOT REGEXP(\'X[0-9]+$\') GROUP by psffitting_current.name',0),
	(1,'debris.uns_multiple_comp_crossref','Herschel DEBRIS',NULL,'debris.uns_multiple_comp_crossref.sourceid LEFT JOIN debris.psffitting_current ON CONCAT(unsid,comps)=psffitting_current.name','\'PACS160\'','IF(aprad>8,apflux,1.21*flux)','IF(aprad>8,rand_e_aper,1.21*e_flux)','0.065*IF(aprad>8,apflux,flux)','0','\'2018.DEBRIS\'','Jy','CONCAT(\'chisq:\',IFNULL(chisq_dof,\'\'))','CONCAT(\'Phot:\',IF(aprad>8,\'Ap\',\'PSF\'))',NULL,NULL,' AND wav=160 AND name NOT REGEXP(\'X[0-9]+$\') GROUP by psffitting_current.name',0),
	(1,'debris.uns_multiple_comp_crossref','Herschel DEBRIS',NULL,'debris.uns_multiple_comp_crossref.sourceid LEFT JOIN debris.psffitting_current ON CONCAT(unsid,comps)=psffitting_current.name','\'PACS70\'','IF(aprad>4,apflux,1.16*flux)','IF(aprad>4,rand_e_aper,1.16*e_flux)','0.0565*IF(aprad>4,apflux,flux)','0','\'2018.DEBRIS\'','Jy','CONCAT(\'chisq:\',IFNULL(chisq_dof,\'\'))','CONCAT(\'Phot:\',IF(aprad>4,\'Ap\',\'PSF\'))',NULL,NULL,' AND wav=70 AND name NOT REGEXP(\'X[0-9]+$\') GROUP by psffitting_current.name',0),
	(1,'debris.uns_multiple_comp_crossref','Herschel DEBRIS',NULL,'debris.uns_multiple_comp_crossref.sourceid LEFT JOIN debris.psffitting_current ON CONCAT(unsid,comps)=psffitting_current.name','\'SPIRE250\'','IF(aprad>22,apflux,flux)','IF(aprad>22,rand_e_aper,e_flux)','GREATEST(0.07*flux,0.007)','0','\'2018.DEBRIS\'','Jy','CONCAT(\'chisq:\',IFNULL(chisq_dof,\'\'))','CONCAT(\'Cov:\',IFNULL(coverage,\'\'))',NULL,NULL,' AND wav=250 AND name NOT REGEXP(\'X[0-9]+$\') GROUP by psffitting_current.name',0),
	(1,'debris.uns_multiple_comp_crossref','Herschel DEBRIS',NULL,'debris.uns_multiple_comp_crossref.sourceid LEFT JOIN debris.psffitting_current ON CONCAT(unsid,comps)=psffitting_current.name','\'SPIRE350\'','IF(aprad>30,apflux,flux)','IF(aprad>30,rand_e_aper,e_flux)','GREATEST(0.07*flux,0.007)','0','\'2018.DEBRIS\'','Jy','CONCAT(\'chisq:\',IFNULL(chisq_dof,\'\'))','CONCAT(\'Cov:\',IFNULL(coverage,\'\'))',NULL,NULL,' AND wav=350 AND name NOT REGEXP(\'X[0-9]+$\') GROUP by psffitting_current.name',0),
	(1,'debris.uns_multiple_comp_crossref','Herschel DEBRIS',NULL,'debris.uns_multiple_comp_crossref.sourceid LEFT JOIN debris.psffitting_current ON CONCAT(unsid,comps)=psffitting_current.name','\'SPIRE500\'','IF(aprad>42,apflux,flux)','IF(aprad>42,rand_e_aper,e_flux)','GREATEST(0.07*flux,0.007)','0','\'2018.DEBRIS\'','Jy','CONCAT(\'chisq:\',IFNULL(chisq_dof,\'\'))','CONCAT(\'Cov:\',IFNULL(coverage,\'\'))',NULL,NULL,' AND wav=500 AND name NOT REGEXP(\'X[0-9]+$\') GROUP by psffitting_current.name',0),
	(-1,'feps',NULL,NULL,'feps.Name','\'IRAC3P6\'','IRAC1','IRAC1_sigint','IRAC1*0.01','0','\'2008ApJS..179..423C\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'feps',NULL,NULL,'feps.Name','\'IRAC4P5\'','IRAC2','IRAC2_sigint','IRAC2*0.01','0','\'2008ApJS..179..423C\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'feps',NULL,NULL,'feps.Name','\'IRAC5P8\'','IRAC3','IRAC3_sigint','IRAC3*0.01','0','\'2008ApJS..179..423C\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'feps',NULL,NULL,'feps.Name','\'IRAC8\'','IRAC4','IRAC4_sigint','IRAC4*0.01','0','\'2008ApJS..179..423C\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(0,'feps',NULL,NULL,'feps.Name','\'MIPS160\'','MIPS160','MIPS160_sigint','MIPS160*0.12','0','\'2008ApJS..179..423C\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'feps',NULL,NULL,'feps.Name','\'MIPS24\'','MIPS24','MIPS24_sigint','MIPS24*0.01','0','\'2008ApJS..179..423C\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'gaspar13',NULL,NULL,'concat(substring(gaspar13.name,1,locate(\' \',gaspar13.name)),substring(gaspar13.name,locate(\' \',gaspar13.name)+1,10)*1)','\'MIPS24\'','f24','e24','f24*0.01','0','\'2013ApJ...768...25G\'','mJy','CONCAT(\'Xs:\',IFNULL(xs,\'\'))','CONCAT(\'AgeFlg:\',IFNULL(age_flag,\'\'))',NULL,NULL,NULL,0),
	(-1,'gautier07',NULL,NULL,'GJ_ID','\'MIPS24\'','F24','e_F24','F24*0.01','0','\'2007ApJ...667..527G\'','mJy','CONCAT(\'SpTy:\',IFNULL(spec_type,\'\'))','CONCAT(\'Name:\',IFNULL(commonName,\'\'))',NULL,NULL,NULL,0),
	(1,'herscheldb.name2simbad','Herschel Archive',NULL,'herscheldb.name2simbad.simbad LEFT JOIN herscheldb.psffitting on psffitting.name=name2simbad.name','\'PACS100\'','IF(aprad>5,apflux_corrected,1.19*flux_uncorrected)','IF(aprad>5,rand_e_aper,1.19*e_flux)','0.03*IF(aprad>5,apflux_corrected,flux_uncorrected)','0','\'HSA_GMK\'','Jy','psffitting.ObsId','CONCAT(\'Phot:\',IF(aprad>5,\'Ap\',\'PSF\'))',NULL,NULL,' AND wav=100 AND flux_uncorrected IS NOT NULL',0),
	(1,'herscheldb.name2simbad','Herschel Archive',NULL,'herscheldb.name2simbad.simbad LEFT JOIN herscheldb.psffitting on psffitting.name=name2simbad.name','\'PACS160\'','IF(aprad>8,apflux_corrected,1.21*flux_uncorrected)','IF(aprad>8,rand_e_aper,1.21*e_flux)','0.05*IF(aprad>8,apflux_corrected,flux_uncorrected)','0','\'HSA_GMK\'','Jy','psffitting.ObsId','CONCAT(\'Phot:\',IF(aprad>8,\'Ap\',\'PSF\'))',NULL,NULL,' AND wav=160 AND flux_uncorrected IS NOT NULL',0),
	(1,'herscheldb.name2simbad','Herschel Archive',NULL,'herscheldb.name2simbad.simbad LEFT JOIN herscheldb.psffitting on psffitting.name=name2simbad.name','\'PACS70\'','IF(aprad>4,apflux_corrected,1.16*flux_uncorrected)','IF(aprad>4,rand_e_aper,1.16*e_flux)','0.03*IF(aprad>4,apflux_corrected,flux_uncorrected)','0','\'HSA_GMK\'','Jy','psffitting.ObsId','CONCAT(\'Phot:\',IF(aprad>4,\'Ap\',\'PSF\'))',NULL,NULL,' AND wav=70 AND flux_uncorrected IS NOT NULL',0),
	(-1,'koerner10_tab1',NULL,NULL,'HIP_ID','\'MIPS24\'','F_MIPS24','e_F_MIPS24','F_MIPS24*0.01','0','\'2010ApJ...710L..26K\'','Jy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'low05',NULL,NULL,'low05.star','\'MIPS24\'','F24','e_F24','F24*0.01','0','\'2005ApJ...631.1170L\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'moor10',NULL,NULL,'SourceID','\'MIPS24\'','F24','e_F24','F24*0.01','0','\'2011ApJS..193....4M\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'morales09_tab3a',NULL,NULL,'HD_ID','\'MIPS24\'','F_24mJy','e_F24','F_24mJy*0.01','0','\'2009ApJ...699.1067M\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'morales09_tab3b',NULL,NULL,'HD_ID','\'MIPS24\'','F24','e_F24','F24*0.01','0','\'2009ApJ...699.1067M\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(1,'photometry.bei06b',NULL,NULL,'bei06b.Name','\'MIPS70\'','F70','e_F70','F70*0.04','0','\'2006ApJ...652.1674B\'','mJy','CONCAT(\'Flags:\',IFNULL(flags,\'\'))','CONCAT(\'Altname:\',IFNULL(name_old,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.bes90',NULL,NULL,'GJ','\'BJ_VJ\'','B_V','0','0.02','0','\'1990A&AS...83..357B\'','mag','CONCAT(\'Std?:\',IFNULL(STD,\'\'))','CONCAT(\'Vmag:\',IFNULL(V,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.bes90',NULL,NULL,'GJ','\'VJ\'','V','0','0.02','0','\'1990A&AS...83..357B\'','mag','CONCAT(\'Std?:\',IFNULL(STD,\'\'))','CONCAT(\'Vmag:\',IFNULL(V,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.bes90',NULL,NULL,'GJ','\'VJ_IC\'','V_I','0','0.02','0','\'1990A&AS...83..357B\'','mag','CONCAT(\'Std?:\',IFNULL(STD,\'\'))','CONCAT(\'Vmag:\',IFNULL(V,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.bes90',NULL,NULL,'GJ','\'VJ_RC\'','V_R','0','0.02','0','\'1990A&AS...83..357B\'','mag','CONCAT(\'Std?:\',IFNULL(STD,\'\'))','CONCAT(\'Vmag:\',IFNULL(V,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.bry06',NULL,NULL,'HD','\'MIPS70\'','F70','e_F70','F70*0.04','0','\'2006ApJ...636.1098B\'','mJy','CONCAT(\'HD:\',IFNULL(HD,\'\'))','CONCAT(\'fHD:\',IFNULL(f_HD,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.bryden09',NULL,NULL,'StarName','\'MIPS70\'','F_70','e_F70','F_70*0.04','0','\'2009ApJ...705.1226B\'','mJy','CONCAT(\'Star:\',IFNULL(Star,\'\'))','CONCAT(\'Note:\',IFNULL(Note,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.carp09_us',NULL,NULL,'carp09_us.Name1','\'MIPS70\'','S70','e_S70','S70*0.04','0','\'2009ApJ...705.1646C\'','mJy','CONCAT(\'AOR:\',IFNULL(AORKEY,\'\'))','CONCAT(\'Name:\',IFNULL(carp09_us.Name,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.chen12',NULL,NULL,'chen12.Name','\'MIPS70\'','F70','e_F70','F70*0.07','IF(l_F70=\'<\',1,0)','\'2012ApJ...756..133C\'','mJy','CONCAT(\'24 Contam:\',f_F24)','CONCAT(\'Notes:\',IFNULL(Notes,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.chen14_t2',NULL,NULL,'chen14_t2.ID','\'MIPS70\'','F70','e_F70','F70*0.07','IF(l_F70=\'<\',1,0)','\'2014ApJS..211...25C\'','mJy','CONCAT(\'Name:\',chen14_t2.Name)','CONCAT(\'r_F70:\',IFNULL(r_F70,\'\'))',NULL,'ID',NULL,0),
	(1,'photometry.feps','Spitzer FEPS',NULL,'feps.Name','\'MIPS70\'','MIPS70','MIPS70_sigint','MIPS70*0.04','0','\'2008ApJS..179..423C\'','mJy','CONCAT(\'Name\',Name)','CONCAT(\'Flag70:\',IFNULL(Flag_70,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.gaspar13',NULL,NULL,'concat(substring(gaspar13.name,1,locate(\' \',gaspar13.name)),substring(gaspar13.name,locate(\' \',gaspar13.name)+1,10)*1)','\'MIPS70\'','f70','e70','f70*0.05','0','\'2013ApJ...768...25G\'','mJy','CONCAT(\'Xs:\',IFNULL(xs,\'\'))','CONCAT(\'AgeFlg:\',IFNULL(age_flag,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.gautier07',NULL,NULL,'gautier07.name','\'MIPS70\'','F70','e_F70','F70*0.04','0','\'2007ApJ...667..527G\'','mJy','CONCAT(\'SpTy:\',IFNULL(spec_type,\'\'))','CONCAT(\'Name:\',IFNULL(commonName,\'\'))',NULL,NULL,NULL,0),
	(0,'photometry.gliese91',NULL,NULL,'gliese91.Name','\'BJ_VJ\'','B_V','0','0.02','0','\'1995yCat.5070....0G\'','mag','CONCAT(\'Comp:\',IFNULL(Comp,\'\'))','CONCAT(\'Sp:\',IFNULL(Sp,\'\'))',NULL,NULL,NULL,0),
	(0,'photometry.gliese91',NULL,NULL,'gliese91.Name','\'RC_IC\'','R_I','0','0.02','0','\'1995yCat.5070....0G\'','mag','CONCAT(\'Comp:\',IFNULL(Comp,\'\'))','CONCAT(\'Sp:\',IFNULL(Sp,\'\'))',NULL,NULL,NULL,0),
	(0,'photometry.gliese91',NULL,NULL,'gliese91.Name','\'UJ_BJ\'','U_B','0','0.02','0','\'1995yCat.5070....0G\'','mag','CONCAT(\'Comp:\',IFNULL(Comp,\'\'))','CONCAT(\'Sp:\',IFNULL(Sp,\'\'))',NULL,NULL,NULL,0),
	(0,'photometry.gliese91',NULL,NULL,'gliese91.Name','\'VJ\'','Vmag','0','0.02','0','\'1995yCat.5070....0G\'','mag','CONCAT(\'Comp:\',IFNULL(Comp,\'\'))','CONCAT(\'Sp:\',IFNULL(Sp,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.HIP','Hipparcos','$H_p$','HIP_ID','\'HP\'','Hpmag','sqrt(e_Hpmag*e_Hpmag+Hpscat*Hpscat)','0.005','0','\'1997HIP...C......0E\'','mag','CONCAT(\'VarType:\',IFNULL(HvarType,\'\'))','CONCAT(\'Mult:\',IFNULL(MultFlag,\'\'))','IF(HvarType is NULL or HvarType=\'C\' or HvarType = \'M\',0,1)',NULL,NULL,0),
	(1,'photometry.HPESL_v1','Herschel PACS XSC',NULL,'HPESL_v1.name','\'PACS100\'','flux','flux/snr','0.05*flux','0','\'2017arXiv170505693M\'','mJy','CONCAT(\'Elong:\',IFNULL(ROUND(flag_elong,4),\'\'))','CONCAT(\'FWHMx:\',IFNULL(ROUND(fwhmxfit,2),\'\'))',NULL,NULL,'AND band = \'green\'',0),
	(1,'photometry.HPESL_v1','Herschel PACS XSC',NULL,'HPESL_v1.name','\'PACS160\'','flux','flux/snr','0.05*flux','0','\'2017arXiv170505693M\'','mJy','CONCAT(\'Elong:\',IFNULL(ROUND(flag_elong,4),\'\'))','CONCAT(\'FWHMx:\',IFNULL(ROUND(fwhmxfit,2),\'\'))',NULL,NULL,'AND band = \'red\'',0),
	(1,'photometry.HPESL_v1','Herschel PACS XSC',NULL,'HPESL_v1.name','\'PACS70\'','flux','flux/snr','0.05*flux','0','\'2017arXiv170505693M\'','mJy','CONCAT(\'Elong:\',IFNULL(ROUND(flag_elong,4),\'\'))','CONCAT(\'FWHMx:\',IFNULL(ROUND(fwhmxfit,2),\'\'))',NULL,NULL,'AND band = \'blue\'',0),
	(1,'photometry.HPPSC_070_v1','Herschel PACS PSC',NULL,'HPPSC_070_v1.name','\'PACS70\'','flux','snrnoise','0.03*flux','0','\'2017arXiv170505693M\'','mJy','CONCAT(\'Elong:\',IFNULL(ROUND(flag_elong,4),\'\'))','CONCAT(\'Blend:\',IFNULL(flag_blend,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.HPPSC_100_v1','Herschel PACS PSC',NULL,'HPPSC_100_v1.name','\'PACS100\'','flux','snrnoise','0.05*flux','0','\'2017arXiv170505693M\'','mJy','CONCAT(\'Elong:\',IFNULL(ROUND(flag_elong,4),\'\'))','CONCAT(\'Blend:\',IFNULL(flag_blend,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.HPPSC_160_v1','Herschel PACS PSC',NULL,'HPPSC_160_v1.name','\'PACS160\'','flux','snrnoise','0.03*flux','0','\'2017arXiv170505693M\'','mJy','CONCAT(\'Elong:\',IFNULL(ROUND(flag_elong,4),\'\'))','CONCAT(\'Blend:\',IFNULL(flag_blend,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_FSC','IRAS FSC',NULL,'IRAS_ID','\'IRAS100\'','Fnu100','e_Fnu100*Fnu100/100.0','0','IF(q_Fnu100 = 1,1,0)','\'1990IRASF.C......0M\'','Jy','CONCAT(\'Qual:\',q_Fnu100)','CONCAT(\'Conf:\',IFNULL(conf,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_FSC','IRAS FSC',NULL,'IRAS_ID','\'IRAS12\'','Fnu12','e_Fnu12*Fnu12/100.0','0','IF(q_Fnu12 = 1,1,0)','\'1990IRASF.C......0M\'','Jy','CONCAT(\'Qual:\',q_Fnu12)','CONCAT(\'Conf:\',IFNULL(conf,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_FSC','IRAS FSC',NULL,'IRAS_ID','\'IRAS25\'','Fnu25','e_Fnu25*Fnu25/100.0','0','IF(q_Fnu25 = 1,1,0)','\'1990IRASF.C......0M\'','Jy','CONCAT(\'Qual:\',q_Fnu25)','CONCAT(\'Conf:\',IFNULL(conf,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_FSC','IRAS FSC',NULL,'IRAS_ID','\'IRAS60\'','Fnu60','e_Fnu60*Fnu60/100.0','0','IF(q_Fnu60 = 1,1,0)','\'1990IRASF.C......0M\'','Jy','CONCAT(\'Qual:\',q_Fnu60)','CONCAT(\'Conf:\',IFNULL(conf,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_PSC','IRAS PSC',NULL,'IRAS_ID','\'IRAS100\'','Fnu_100','e_Fnu_100*Fnu_100/100.0','0','IF(q_Fnu_100 = 1,1,0)','\'1988IRASP.C......0J\'','Jy','CONCAT(\'Qual:\',q_Fnu_100)','CONCAT(\'Conf:\',IFNULL(confuse,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_PSC','IRAS PSC',NULL,'IRAS_ID','\'IRAS12\'','Fnu_12','e_Fnu_12*Fnu_12/100.0','0','IF(q_Fnu_12 = 1,1,0)','\'1988IRASP.C......0J\'','Jy','CONCAT(\'Qual:\',q_Fnu_12)','CONCAT(\'Conf:\',IFNULL(confuse,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_PSC','IRAS PSC',NULL,'IRAS_ID','\'IRAS25\'','Fnu_25','e_Fnu_25*Fnu_25/100.0','0','IF(q_Fnu_25 = 1,1,0)','\'1988IRASP.C......0J\'','Jy','CONCAT(\'Qual:\',q_Fnu_25)','CONCAT(\'Conf:\',IFNULL(confuse,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.IRAS_PSC','IRAS PSC',NULL,'IRAS_ID','\'IRAS60\'','Fnu_60','e_Fnu_60*Fnu_60/100.0','0','IF(q_Fnu_60 = 1,1,0)','\'1988IRASP.C......0J\'','Jy','CONCAT(\'Qual:\',q_Fnu_60)','CONCAT(\'Conf:\',IFNULL(confuse,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.kimeswenger_2004_',NULL,'$J$','kimeswenger_2004_.name','\'JDENIS\'','jmag','e_jmag','0.01','0','\'2004A&A...413.1037K\'','mag','CONCAT(\'Var:\',IFNULL(var,\'\'))','CONCAT(\'Note:\',IFNULL(rem,\'\'))','var > 3',NULL,NULL,0),
	(1,'photometry.kimeswenger_2004_',NULL,'$K_s$','kimeswenger_2004_.name','\'KSDENIS\'','ksmag','e_ksmag','0.01','0','\'2004A&A...413.1037K\'','mag','CONCAT(\'Var:\',IFNULL(var,\'\'))','CONCAT(\'Note:\',IFNULL(rem,\'\'))','var > 3',NULL,NULL,0),
	(1,'photometry.koen02',NULL,NULL,'HIP_ID','\'BJ_VJ\'','B_V','e_B_V','0.005','0','\'2010MNRAS.403.1949K\'','mag','CONCAT(\'o_Vmag:\',IFNULL(o_Vmag,\'\'))','CONCAT(\'Var:\',IFNULL(Var,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.koen02',NULL,NULL,'HIP_ID','\'UJ_BJ\'','U_B','e_U_B','0.005','0','\'2010MNRAS.403.1949K\'','mag','CONCAT(\'o_Vmag:\',IFNULL(o_Vmag,\'\'))','CONCAT(\'Var:\',IFNULL(Var,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.koen02',NULL,NULL,'HIP_ID','\'VJ\'','Vmag','e_Vmag','0.005','0','\'2010MNRAS.403.1949K\'','mag','CONCAT(\'o_Vmag:\',IFNULL(o_Vmag,\'\'))','CONCAT(\'Var:\',IFNULL(Var,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.koerner10_tab2',NULL,NULL,'HIP_ID','\'MIPS70\'','F_MIPS70','e_F_MIPS70','F_MIPS70*0.04','0','\'2010ApJ...710L..26K\'','Jy','CONCAT(\'SpT:\',IFNULL(SpT,\'\'))','CONCAT(\'HIP:\',IFNULL(HIP,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.low05',NULL,NULL,'low05.star','\'MIPS70\'','F70','e_F70','F70*0.04','IF(lim70 IS NOT NULL,1,0)','\'2005ApJ...631.1170L\'','mJy','CONCAT(\'Star:\',IFNULL(star,\'\'))','CONCAT(\'epoch:\',IFNULL(epoch,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.moor10',NULL,NULL,'SourceID','\'MIPS70\'','F70','e_F70','F70*0.04','0','\'2011ApJS..193....4M\'','mJy','CONCAT(\'AOR:\',IFNULL(AOR,\'\'))','CONCAT(\'Notes:\',IFNULL(Notes,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.morales09_tab3a',NULL,NULL,'HD_ID','\'MIPS70\'','F70mJy','e_F70','F70mJy*0.04','0','\'2009ApJ...699.1067M\'','mJy','CONCAT(\'Tdust:\',IFNULL(T_dust,\'\'))','CONCAT(\'T-PR:\',IFNULL(T_PR_T_COLL,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.morales09_tab3b',NULL,NULL,'HD_ID','\'MIPS70\'','F70','e_F70','F70*0.04','0','\'2009ApJ...699.1067M\'','mJy','CONCAT(\'Dbl:\',IFNULL(Dbl,\'\'))','CONCAT(\'a:\',IFNULL(alpha,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.paunzen15','Heritage Stromgren','$b-y$','tyc','\'BS_YS\'','b_y','IFNULL(e_b_y,0.008)','0.005','0','\'2015A&A...580A..23P\'','mag','CONCAT(\'Vmag:\',IFNULL(vmag,\'\'))','CONCAT(\'beta:\',IFNULL(beta,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.paunzen15','Heritage Stromgren','$c_1$','tyc','\'STROMC1\'','c1','IFNULL(e_c1,0.01)','0.005','0','\'2015A&A...580A..23P\'','mag','CONCAT(\'Vmag:\',IFNULL(vmag,\'\'))','CONCAT(\'beta:\',IFNULL(beta,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.paunzen15','Heritage Stromgren','$m_1$','tyc','\'STROMM1\'','m1','IFNULL(e_m1,0.012)','0.005','0','\'2015A&A...580A..23P\'','mag','CONCAT(\'Vmag:\',IFNULL(vmag,\'\'))','CONCAT(\'beta:\',IFNULL(beta,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.plav09_tab2',NULL,NULL,'plav09_tab2.ID','\'MIPS70\'','flux_mjy_70','flux_mJy_70_error','flux_mjy_70*0.01','IF(flux_70_limit=\'<\',1,0)','\'2009ApJ...698.1068P\'','mJy','CONCAT(\'Name:\',IFNULL(simbad_name,\'\'))','CONCAT(\'new_xs:\',IFNULL(new_xs,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.rebull08_bpmg',NULL,NULL,'ID_USE','\'MIPS70\'','flux_mjy_70','0','flux_mjy_70*0.04','IF(limit_flag_70=\'<\',1,0)','\'2008ApJ...681.1484R\'','mJy','CONCAT(\'ID:\',IFNULL(ID_OTHER,\'\'))','CONCAT(\'Vmag:\',IFNULL(MAG_V,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.spsc_standard_250_v2','Herschel SPIRE PSC',NULL,'spsc_standard_250_v2.name','\'SPIRE250\'','IF(extsrc_flag=1,fluxtm2,flux)','IF(extsrc_flag=1,fluxtm2_err,flux_err)','0.07 * IF(extsrc_flag=1,fluxtm2,flux)','0','\'2017.HSPSC_v1\'','mJy','CONCAT(\'Ext:\',IF(extsrc_flag=1,\'1\',\'0\'))','CONCAT(\'Low FWHM:\',IF(lowfwhm_flag=1,\'1\',\'0\'))',NULL,NULL,NULL,0),
	(1,'photometry.spsc_standard_350_v2','Herschel SPIRE PSC',NULL,'spsc_standard_350_v2.name','\'SPIRE350\'','IF(extsrc_flag=1,fluxtm2,flux)','IF(extsrc_flag=1,fluxtm2_err,flux_err)','0.07 * IF(extsrc_flag=1,fluxtm2,flux)','0','\'2017.HSPSC_v1\'','mJy','CONCAT(\'Ext:\',IF(extsrc_flag=1,\'1\',\'0\'))','CONCAT(\'Low FWHM:\',IF(lowfwhm_flag=1,\'1\',\'0\'))',NULL,NULL,NULL,0),
	(1,'photometry.spsc_standard_500_v2','Herschel SPIRE PSC',NULL,'spsc_standard_500_v2.name','\'SPIRE500\'','IF(extsrc_flag=1,fluxtm2,flux)','IF(extsrc_flag=1,fluxtm2_err,flux_err)','0.07 * IF(extsrc_flag=1,fluxtm2,flux)','0','\'2017.HSPSC_v1\'','mJy','CONCAT(\'Ext:\',IF(extsrc_flag=1,\'1\',\'0\'))','CONCAT(\'Low FWHM:\',IF(lowfwhm_flag=1,\'1\',\'0\'))',NULL,NULL,NULL,0),
	(1,'photometry.su06',NULL,NULL,'HD_ID','\'MIPS70\'','F70','e_F70','F70*0.04','IF(l_F70=\'<\',1,0)','\'2006ApJ...653..675S\'','mJy','CONCAT(\'Age:\',IFNULL(age,\'\'))','CONCAT(\'xs70:\',IFNULL(IRE70,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.TDSC_main_suppl','Tycho Double Star','$B_T$','TDSC_ID','\'BT\'','BTmag','e_BTmag','0.006','0','\'2002A&A...384..180F\'','mag','CONCAT(\'Comp:\',IFNULL(m_TDSC,\'\'))','CONCAT(\'MagFlg:\',IFNULL(magflg,\'\'))','magflg REGEXP(\'T|B|V|H\')',NULL,NULL,0),
	(1,'photometry.TDSC_main_suppl','Tycho Double Star','$V_T$','TDSC_ID','\'VT\'','VTmag','e_VTmag','0.006','0','\'2002A&A...384..180F\'','mag','CONCAT(\'Comp:\',IFNULL(m_TDSC,\'\'))','CONCAT(\'MagFlg:\',IFNULL(magflg,\'\'))','magflg REGEXP(\'T|B|V|H\')',NULL,NULL,0),
	(1,'photometry.trilling07',NULL,NULL,'trilling07.name','\'MIPS70\'','F70','e_F70','F70*0.04','0','\'2007ApJ...658.1289T\'','mJy','CONCAT(\'Vmag:\',IFNULL(Vmag,\'\'))','CONCAT(\'Kmag:\',IFNULL(Kmag,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.trilling08',NULL,NULL,'trilling08.ID','\'MIPS70\'','F70','e_F70','F70*0.04','0','\'2008ApJ...674.1086T\'','mJy','CONCAT(\'Vmag:\',IFNULL(Vmag,\'\'))','CONCAT(\'Kmag:\',IFNULL(Kmag,\'\'))',NULL,NULL,NULL,0),
	(-1,'photometry.ubvy_mean',NULL,NULL,'ID_fromlid','\'BS_YS\'','b_y','IFNULL(e_b_y,0.01)','0.013','0','\'1998A&AS..129..431H\'','mag','CONCAT(\'Comp:\',IFNULL(m_LID,\'\'))','CONCAT(\'Vmag:\',IFNULL(Vmag,\'\'))',NULL,NULL,NULL,0),
	(-1,'photometry.ubvy_mean',NULL,NULL,'ID_fromlid','\'STROMC1\'','c1','IFNULL(e_c1,0.01)','0.018','0','\'1998A&AS..129..431H\'','mag','CONCAT(\'Comp:\',IFNULL(m_LID,\'\'))','CONCAT(\'Vmag:\',IFNULL(Vmag,\'\'))',NULL,NULL,NULL,0),
	(-1,'photometry.ubvy_mean',NULL,NULL,'ID_fromlid','\'STROMM1\'','m1','IFNULL(e_m1,0.01)','0.014','0','\'1998A&AS..129..431H\'','mag','CONCAT(\'Comp:\',IFNULL(m_LID,\'\'))','CONCAT(\'Vmag:\',IFNULL(Vmag,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.UBV_mean','Heritage UBV','$B-V$','ID_fromlid','\'BJ_VJ\'','B_V','e_B_V','0.02','0','\'2006yCat.2168....0M\'','mag','CONCAT(\'Comp:\',IFNULL(m_LID,\'\'))','CONCAT(\'n_B_V:\',IFNULL(n_B_V,\'\'))','IF(m_lid IS NULL,0,1)',NULL,NULL,0),
	(1,'photometry.UBV_mean','Heritage UBV','$U-B$','ID_fromlid','\'UJ_BJ\'','U_B','e_U_B','0.028','0','\'2006yCat.2168....0M\'','mag','CONCAT(\'Comp:\',IFNULL(m_LID,\'\'))','CONCAT(\'n_U_B:\',IFNULL(n_U_B,\'\'))','IF(m_lid IS NULL,0,1)',NULL,NULL,0),
	(1,'photometry.UBV_mean','Heritage UBV','$V$','ID_fromlid','\'VJ\'','Vmag','e_Vmag','0.02','0','\'2006yCat.2168....0M\'','mag','CONCAT(\'Comp:\',IFNULL(m_LID,\'\'))','CONCAT(\'n_Vmag:\',IFNULL(n_Vmag,\'\'))','IF(m_lid IS NULL,0,1)',NULL,NULL,0),
	(1,'photometry.zuckerman11_tab10',NULL,NULL,'Star','\'MIPS70\'','F70','0','F70*0.04','0','\'2011ApJ...732...61Z\'','mJy','CONCAT(\'xs:\',IFNULL(xs70,\'\'))','CONCAT(\'IRS:\',IFNULL(IRS,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.zuckerman11_tab8',NULL,NULL,'Star','\'MIPS70\'','F70','0','F70*0.04','0','\'2011ApJ...732...61Z\'','mJy','CONCAT(\'xs:\',IFNULL(xs70,\'\'))','CONCAT(\'IRS:\',IFNULL(IRS,\'\'))',NULL,NULL,NULL,0),
	(1,'photometry.zuckerman11_tab9',NULL,NULL,'Star','\'MIPS70\'','F70','0','F70*0.04','0','\'2011ApJ...732...61Z\'','mJy','CONCAT(\'xs:\',IFNULL(xs70,\'\'))','CONCAT(\'IRS:\',IFNULL(IRS,\'\'))',NULL,NULL,NULL,0),
	(-1,'plav09_tab2',NULL,NULL,'plav09_tab2.ID','\'MIPS70\'','flux_mjy_70','flux_mJy_70_error','flux_mjy_70*0.04','IF(flux_70_limit=\'<\',1,0)','\'2009ApJ...698.1068P\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(0,'rebull08_bpmg',NULL,NULL,'ID_USE','\'MIPS160\'','flux_mjy_160','0','flux_mjy_160*0.12','IF(limit_flag_160=\'<\',1,0)','\'2008ApJ...681.1484R\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'rebull08_bpmg',NULL,NULL,'ID_USE','\'MIPS24\'','flux_mjy_24','0','flux_mjy_24*0.01','0','\'2008ApJ...681.1484R\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(1,'sdb.2mass','2MASS PSC','$J$','2mass.sdbid','IF(SUBSTRING(Rflg,1,1)=1,\'2MR1J\',IF(SUBSTRING(Rflg,1,1)=2,\'2MR2J\',\'2MJ\'))','Jmag','e_Jmag','0.01','0','\'2003tmc..book.....C\'','mag','CONCAT(\'Qflg:\',SUBSTR(Qflg,1,1))','CONCAT(\'Cflg:\',SUBSTRING(Cflg,1,1))','IF(SUBSTR(Qflg,1,1) REGEXP(\'A|B|C|D\') AND SUBSTR(Cflg,1,1)=\'0\',0,1)','2mass',NULL,0),
	(1,'sdb.2mass','2MASS PSC','$H$','2mass.sdbid','IF(SUBSTRING(Rflg,2,1)=1,\'2MR1H\',IF(SUBSTRING(Rflg,2,1)=2,\'2MR2H\',\'2MH\'))','Hmag','e_Hmag','0.01','0','\'2003tmc..book.....C\'','mag','CONCAT(\'Qflg:\',SUBSTR(Qflg,2,1))','CONCAT(\'Cflg:\',SUBSTRING(Cflg,2,1))','IF(SUBSTR(Qflg,2,1) REGEXP(\'A|B|C|D\') AND SUBSTR(Cflg,2,1)=\'0\',0,1)',NULL,NULL,0),
	(1,'sdb.2mass','2MASS PSC','$K_s$','2mass.sdbid','IF(SUBSTRING(Rflg,3,1)=1,\'2MR1KS\',IF(SUBSTRING(Rflg,3,1)=2,\'2MR2KS\',\'2MKS\'))','Kmag','e_Kmag','0.01','0','\'2003tmc..book.....C\'','mag','CONCAT(\'Qflg:\',SUBSTR(Qflg,3,1))','CONCAT(\'Cflg:\',SUBSTRING(Cflg,3,1))','IF(SUBSTR(Qflg,3,1) REGEXP(\'A|B|C|D\') AND SUBSTR(Cflg,3,1)=\'0\',0,1)',NULL,NULL,0),
	(1,'sdb.akari_irc','AKARI IRC V1.0','S18W','akari_irc.sdbid','\'AKARI18\'','S18','e_S18','S18*0.015','0','\'2010A&A...514A...1I\'','Jy','CONCAT(\'ext?:\',IFNULL(X18,\'\'))','CONCAT(\'N-near:\',IFNULL(ROUND(Nd18,3),\'\'))','IF(q_S18 = 3,0,1)',NULL,NULL,0),
	(1,'sdb.akari_irc','AKARI IRC V1.0','S9W','akari_irc.sdbid','\'AKARI9\'','S09','e_S09','S09*0.015','0','\'2010A&A...514A...1I\'','Jy','CONCAT(\'ext?:\',IFNULL(X09,\'\'))','CONCAT(\'N-near:\',IFNULL(ROUND(Nd09,3),\'\'))','IF(q_S09 = 3,0,1)',NULL,NULL,0),
	(1,'sdb.allwise','ALLWISE','W3','allwise.sdbid','\'WISE12\'','w3mag','e_w3mag','0.045','IF(SUBSTRING(qph,3,1)=\'U\',1,0)','\'2010AJ....140.1868W\'','mag','CONCAT(\'qual:\',SUBSTRING(qph,3,1))','CONCAT(\'Var:\',IFNULL(var,\'\'))','SUBSTRING(ccf,3,1) REGEXP BINARY \'[A-Z]\'','allwise',NULL,0),
	(1,'sdb.allwise','ALLWISE','W4','allwise.sdbid','\'WISE22\'','w4mag','e_w4mag','0.057','IF(SUBSTRING(qph,4,1)=\'U\',1,0)','\'2010AJ....140.1868W\'','mag','CONCAT(\'qual:\',SUBSTRING(qph,4,1))','CONCAT(\'Var:\',IFNULL(var,\'\'))','SUBSTRING(ccf,4,1) REGEXP BINARY \'[A-Z]\'','allwise',NULL,0),
	(1,'sdb.allwise','ALLWISE','W1','allwise.sdbid','\'WISE3P4\'','w1mag','e_w1mag','0.024','IF(SUBSTRING(qph,1,1)=\'U\',1,0)','\'2010AJ....140.1868W\'','mag','CONCAT(\'qual:\',SUBSTRING(qph,1,1))','CONCAT(\'Var:\',IFNULL(var,\'\'))','SUBSTRING(ccf,1,1) REGEXP BINARY \'[A-Z]\' OR w1mag<4.5','allwise',NULL,0),
	(1,'sdb.allwise','ALLWISE','W2','allwise.sdbid','\'WISE4P6\'','w2mag','e_w2mag','0.028','IF(SUBSTRING(qph,2,1)=\'U\',1,0)','\'2010AJ....140.1868W\'','mag','CONCAT(\'qual:\',SUBSTRING(qph,2,1))','CONCAT(\'Var:\',IFNULL(var,\'\'))','SUBSTRING(ccf,2,1) REGEXP BINARY \'[A-Z]\' OR w2mag<3.0','allwise',NULL,0),
	(1,'sdb.apass','APASS DR9','$B$','apass.sdbid','\'BAPASS\'','Bmag','e_Bmag','0.01','0','\'2016yCat.2336....0H\'','mag','CONCAT(\'nobs:\',nobs)','CONCAT(\'mobs:\',mobs)','Bmag < 7 OR e_Bmag=0',NULL,NULL,0),
	(1,'sdb.apass','APASS DR9','$g$','apass.sdbid','\'GAPASS\'','g_mag','e_g_mag','0.01','0','\'2016yCat.2336....0H\'','mag','CONCAT(\'nobs:\',nobs)','CONCAT(\'mobs:\',mobs)','g_mag < 7 OR e_g_mag=0',NULL,NULL,0),
	(1,'sdb.apass','APASS DR9','$i$','apass.sdbid','\'IAPASS\'','i_mag','e_i_mag','0.01','0','\'2016yCat.2336....0H\'','mag','CONCAT(\'nobs:\',nobs)','CONCAT(\'mobs:\',mobs)','i_mag < 7 OR e_i_mag=0',NULL,NULL,0),
	(1,'sdb.apass','APASS DR9','$r$','apass.sdbid','\'RAPASS\'','r_mag','e_r_mag','0.01','0','\'2016yCat.2336....0H\'','mag','CONCAT(\'nobs:\',nobs)','CONCAT(\'mobs:\',mobs)','r_mag < 7 OR e_r_mag=0',NULL,NULL,0),
	(1,'sdb.apass','APASS DR9','$V$','apass.sdbid','\'VAPASS\'','Vmag','e_Vmag','0.01','0','\'2016yCat.2336....0H\'','mag','CONCAT(\'nobs:\',nobs)','CONCAT(\'mobs:\',mobs)','Vmag < 7 OR e_vmag=0',NULL,NULL,0),
	(1,'sdb.denis','DENIS (2005)','$i$','denis.sdbid','\'IDENIS\'','Imag','e_Imag','0.01','0','\'2005yCat.2263....0T\'','mag','CONCAT(\'Ipsf:\',Ipsf)','CONCAT(\'Iflg:\',Iflg)','Iflg != 0 OR Ipsf <= 90',NULL,NULL,0),
	(1,'sdb.galex','GALEX DR5','FUV','galex.sdbid','\'GALFUV\'','fuv','e_fuv','0.01','0','\'2011Ap&SS.335..161B\'','mag','CONCAT(\'Fafl:\',IFNULL(Fafl,\'\'))','CONCAT(\'Fexf:\',IFNULL(Fexf,\'\'))',NULL,NULL,NULL,0),
	(1,'sdb.galex','GALEX DR5','NUV','galex.sdbid','\'GALNUV\'','nuv','e_nuv','0.01','0','\'2011Ap&SS.335..161B\'','mag','CONCAT(\'Nafl:\',IFNULL(Nafl,\'\'))','CONCAT(\'Nexf:\',IFNULL(Nexf,\'\'))',NULL,NULL,NULL,0),
	(1,'sdb.seip','SEIP',NULL,'seip.sdbid','\'IRAC3P6\'','i1_f_ap1','i1_df_ap1','0.02*i1_f_ap1','0','\'2013.SEIP.dr4\'','uJy','CONCAT(\'Brt:\',IFNULL(i1_brtfrac,\'\'))','CONCAT(\'Ext:\',IFNULL(i1_extfrac,\'\'))','i1_fluxtype != 1',NULL,NULL,0),
	(1,'sdb.seip','SEIP',NULL,'seip.sdbid','\'IRAC4P5\'','i2_f_ap1','i2_df_ap1','0.02*i2_f_ap1','0','\'2013.SEIP.dr4\'','uJy','CONCAT(\'Brt:\',IFNULL(i2_brtfrac,\'\'))','CONCAT(\'Ext:\',IFNULL(i2_extfrac,\'\'))','i2_fluxtype != 1',NULL,NULL,0),
	(1,'sdb.seip','SEIP',NULL,'seip.sdbid','\'IRAC5P8\'','i3_f_ap1','i3_df_ap1','0.02*i3_f_ap1','0','\'2013.SEIP.dr4\'','uJy','CONCAT(\'Brt:\',IFNULL(i3_brtfrac,\'\'))','CONCAT(\'Ext:\',IFNULL(i3_extfrac,\'\'))','i3_fluxtype != 1',NULL,NULL,0),
	(1,'sdb.seip','SEIP',NULL,'seip.sdbid','\'IRAC8\'','i4_f_ap1','i4_df_ap1','0.02*i4_f_ap1','0','\'2013.SEIP.dr4\'','uJy','CONCAT(\'Brt:\',IFNULL(i4_brtfrac,\'\'))','CONCAT(\'Ext:\',IFNULL(i4_extfrac,\'\'))','i4_fluxtype != 1',NULL,NULL,0),
	(1,'sdb.seip','SEIP',NULL,'seip.sdbid','\'MIPS24\'','m1_f_psf','m1_df_psf','0.02*m1_f_psf','0','\'2013.SEIP.dr4\'','uJy','CONCAT(\'Brt:\',IFNULL(m1_brtfrac,\'\'))','CONCAT(\'Ext:\',IFNULL(m1_extfrac,\'\'))','m1_fluxtype != 1',NULL,NULL,0),
	(1,'sdb.tyc2','Tycho-2','$B_T$','tyc2.sdbid','\'BT\'','BTmag','e_BTmag','0.006','0','\'2000A&A...355L..27H\'','mag','CONCAT(\'Prox:\',IFNULL(prox,\'\'))','CONCAT(\'HIP:\',IFNULL(HIP,\'\'))',NULL,NULL,NULL,0),
	(1,'sdb.tyc2','Tycho-2','$V_T$','tyc2.sdbid','\'VT\'','VTmag','e_VTmag','0.006','0','\'2000A&A...355L..27H\'','mag','CONCAT(\'Prox:\',IFNULL(prox,\'\'))','CONCAT(\'HIP:\',IFNULL(HIP,\'\'))',NULL,NULL,NULL,0),
	(1,'sons.SONS_results','JCMT SONS',NULL,'id','\'WAV450\'','IF(Peak_Flux_450_ IS NULL,3*Peak_Flux_error_450_,IF(Disk_450_=\'P\',Peak_Flux_450_,Int_Flux_450_))','IF(Disk_450_=\'P\',Peak_Flux_error_450_,Int_flux_error_450_)','0','IF(Upper_Limit_450_=\'Y\',1,0)','\'2017arXiv170601218H\'','mJy','CONCAT(\'No:\',IFNULL(no_,\'\'))','CONCAT(\'Assoc:\',IFNULL(association,\'\'))','Time_ IS NULL',NULL,NULL,0),
	(1,'sons.SONS_results','JCMT SONS',NULL,'id','\'WAV850\'','IF(Peak_Flux_850_ IS NULL,3*Peak_Flux_error_850_,IF(Disk_850_=\'P\',Peak_Flux_850_,Int_Flux_850_))','IF(Disk_850_=\'P\',Peak_Flux_error_850_,Int_flux_error_850_)','0','IF(Upper_Limit_850_=\'Y\',1,0)','\'2017arXiv170601218H\'','mJy','CONCAT(\'No:\',IFNULL(no_,\'\'))','CONCAT(\'Assoc:\',IFNULL(association,\'\'))','Time_ IS NULL',NULL,NULL,0),
	(-1,'su06',NULL,NULL,'HD_ID','\'MIPS24\'','F24','e_F24','F24*0.01','0','\'2006ApJ...653..675S\'','mJy','CONCAT(\'Age:\',IFNULL(age,\'\'))','\'\'',NULL,NULL,NULL,0),
	(-1,'trilling07',NULL,NULL,'trilling07.name','\'MIPS24\'','F24','e_F24','F24*0.01','0','\'2007ApJ...658.1289T\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'trilling08',NULL,NULL,'trilling08.ID','\'MIPS24\'','F24','0','F24*0.01','0','\'2008ApJ...674.1086T\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'zuckerman11_tab10',NULL,NULL,'Star','\'MIPS24\'','F24','0','F24*0.01','0','\'2011ApJ...732...61Z\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'zuckerman11_tab8',NULL,NULL,'Star','\'MIPS24\'','F24','0','F24*0.01','0','\'2011ApJ...732...61Z\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0),
	(-1,'zuckerman11_tab9',NULL,NULL,'Star','\'MIPS24\'','F24','0','F24*0.01','0','\'2011ApJ...732...61Z\'','mJy','\'\'','\'\'',NULL,NULL,NULL,0);

/*!40000 ALTER TABLE `xmatch` ENABLE KEYS */;
UNLOCK TABLES;



/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;
/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
