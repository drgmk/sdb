/* KEYS - run this after recreating all tables with dropcreate, make sure this is done
   with something found for all tables to get field lengths correct (hd181327). then change
   to append and run once without exiting for duplicates, then uncomment and go ahead... */

ALTER TABLE sdb_pm ADD PRIMARY KEY (sdbid);

ALTER TABLE projects DROP FOREIGN KEY sdbid_pr;
ALTER TABLE projects DROP PRIMARY KEY;
ALTER TABLE projects ADD PRIMARY KEY (sdbid,project);
ALTER TABLE projects ADD CONSTRAINT sdbid_pr FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

/* no foreign key for xids as xid is case sensitive (different character set) */
ALTER TABLE xids DROP INDEX sdbid_sdbid;
ALTER TABLE xids DROP INDEX sdbid_xid;
ALTER TABLE xids DROP PRIMARY KEY;
ALTER TABLE xids ADD PRIMARY KEY (sdbid,xid);
ALTER TABLE xids ADD INDEX sdbid_sdbid (sdbid);
ALTER TABLE xids ADD UNIQUE sdbid_xid (xid);

ALTER TABLE 2mass DROP FOREIGN KEY sdbid_2m;
ALTER TABLE 2mass DROP PRIMARY KEY;
ALTER TABLE 2mass ADD PRIMARY KEY sdbid_2m (sdbid);
ALTER TABLE 2mass ADD CONSTRAINT sdbid_2m FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

ALTER TABLE akari_irc DROP FOREIGN KEY sdbid_ak;
ALTER TABLE akari_irc DROP PRIMARY KEY;
ALTER TABLE akari_irc ADD PRIMARY KEY sdbid_ak (sdbid);
ALTER TABLE akari_irc ADD CONSTRAINT sdbid_ak FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

ALTER TABLE allwise DROP FOREIGN KEY sdbid_aw;
ALTER TABLE allwise DROP PRIMARY KEY;
ALTER TABLE allwise ADD PRIMARY KEY sdbid_aw (sdbid);
ALTER TABLE allwise ADD CONSTRAINT sdbid_aw FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

ALTER TABLE seip DROP FOREIGN KEY sdbid_sp;
ALTER TABLE seip DROP PRIMARY KEY;
ALTER TABLE seip ADD PRIMARY KEY sdbid_sp (sdbid);
ALTER TABLE seip ADD CONSTRAINT sdbid_sp FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

ALTER TABLE tyc2 DROP FOREIGN KEY sdbid_ty;
ALTER TABLE tyc2 DROP PRIMARY KEY;
ALTER TABLE tyc2 ADD PRIMARY KEY sdbid_ty (sdbid);
ALTER TABLE tyc2 ADD CONSTRAINT sdbid_ty FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

ALTER TABLE simbad DROP FOREIGN KEY sdbid_sm;
ALTER TABLE simbad DROP PRIMARY KEY;
ALTER TABLE simbad ADD PRIMARY KEY sdbid_sm (sdbid);
ALTER TABLE simbad ADD CONSTRAINT sdbid_sm FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);
ALTER TABLE simbad MODIFY main_id VARCHAR(100);
ALTER TABLE simbad MODIFY sp_type VARCHAR(36);
ALTER TABLE simbad MODIFY sp_bibcode VARCHAR(19);
ALTER TABLE simbad MODIFY plx_bibcode VARCHAR(19);

ALTER TABLE spectra DROP FOREIGN KEY sdbid_spec;
ALTER TABLE spectra drop PRIMARY KEY;
ALTER TABLE spectra ADD PRIMARY KEY sdbid_spec (sdbid,instrument,aor_key);
ALTER TABLE spectra ADD CONSTRAINT sdbid_spec FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

ALTER TABLE xmatch DROP PRIMARY KEY;
ALTER TABLE xmatch ADD PRIMARY KEY (`table`,band);

/* remove extra whitespace on names put in xids */
CREATE TRIGGER xid_remove_whitespace BEFORE INSERT ON `sed_db`.xids FOR EACH ROW SET NEW.xid=trim(replace(replace(replace(replace(replace(NEW.xid,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' '));
/* same for names in simbad table */
CREATE TRIGGER simbad_remove_whitespace BEFORE INSERT ON `sed_db`.simbad FOR EACH ROW SET NEW.main_id=trim(replace(replace(replace(replace(replace(NEW.main_id,'  ',' '),'  ',' '),'  ',' '),'  ',' '),'  ',' '));


/* CODE */

/* sanity checking to see if an sdbid was entered but nothing else */
select sdb_pm.sdbid,2mass,allwise,main_id,tyc1 from sdb_pm LEFT JOIN simbad USING (sdbid)
LEFT JOIN 2mass USING (sdbid) LEFT JOIN allwise USING (sdbid) LEFT JOIN akari_irc USING
(sdbid) LEFT JOIN tyc2 USING (sdbid) where coalesce(2mass,allwise,main_id,tyc1) is null

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
alter table sample_shardds add column sdbid varchar(25);
update sample_shardds left join xids on name=xid set sample_shardds.sdbid=xids.sdbid;
ALTER TABLE sample_shardds ADD FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);

delete from projects where project='shardds';
insert into projects select sdbid,'shardds' from sample_shardds where sdbid is not null;

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

CREATE TABLE `seip` (
  `sdbid` varchar(25) DEFAULT NULL,
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
  UNIQUE KEY `sdbid_sp` (`sdbid`),
  CONSTRAINT `sdbid_sp` FOREIGN KEY (`sdbid`) REFERENCES `sdb_pm` (`sdbid`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;
