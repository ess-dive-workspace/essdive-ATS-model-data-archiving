"""Behavioral tests for the ATS MDA workflow. Run: python -m unittest discover -s tests -v"""
import hashlib
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ats_mda_utils as workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source'
        self.stage = self.root/'stage'
        self.source.mkdir(); self.stage.mkdir()
        self.cfg = workflow.Config(self.source, self.stage, log_to_file=False, download_definitions=False)
        self.log = io.StringIO()
        workflow.configure_logging(self.cfg, stream=self.log)

    def file(self, name, content='data', source=False):
        path = (self.source if source else self.stage)/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def obs(self, name='run0/water_balance.csv', extra=''):
        return self.file(name, extra+'time [d],"water_flux, outlet [mol/s]"\n0,2\n')

    def prepare_metadata(self):
        self.obs()
        workflow.generate_flmd(self.cfg)
        workflow.generate_dictionary(self.cfg)

    def table(self, name):
        return pd.read_csv(self.stage/name, keep_default_na=False, dtype=str)

    def test_notebook_code_compiles(self):
        n=json.loads((Path(workflow.__file__).parent/'ats_mda_workflow.ipynb').read_text(encoding='utf-8'))
        for i,c in enumerate(n['cells']):
            if c['cell_type']=='code': compile(''.join(c['source']),f'cell {i}','exec')

    def test_rejects_same_and_nested_paths(self):
        for source,stage in [(self.source,self.source),(self.source,self.source/'child'),(self.root,self.stage)]:
            with self.subTest(source=source,stage=stage),self.assertRaisesRegex(ValueError,'non-nested'):
                workflow.validate_workspace(replace(self.cfg,simulation_dir=source,data_pkg_dir=stage))

    def test_missing_source(self):
        with self.assertRaises(FileNotFoundError):
            workflow.validate_workspace(replace(self.cfg,simulation_dir=self.root/'absent'))

    def test_bad_options(self):
        for changes in [dict(cleanup_mode='yes'),dict(run_tokens='run0'),dict(header_line_hint=-1),dict(progress_seconds=0),dict(split_size_gib=0),dict(network_attempts=0),dict(dd_file_name='../bad_dd.csv'),dict(keep_checkpoint_token='')]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                workflow.validate_options(replace(self.cfg,**changes))

    def test_copy_filters_reuses_and_preserves_source(self):
        a=self.file('run0/model.xml','<xml/>',source=True)
        self.file('run0/ignored.bin','ignored',source=True)
        self.assertEqual(workflow.copy_files(self.cfg),{'copied':1,'reused':0})
        self.assertFalse((self.stage/'run0/ignored.bin').exists())
        self.assertEqual(workflow.copy_files(self.cfg),{'copied':0,'reused':1})
        self.assertEqual(a.read_text(),'<xml/>')

    def test_copy_conflict_stops_before_any_new_copy(self):
        self.file('a.xml','new',source=True); self.file('z.xml','new',source=True)
        self.file('z.xml','edited')
        with self.assertRaises(FileExistsError): workflow.copy_files(self.cfg)
        self.assertFalse((self.stage/'a.xml').exists())
        self.assertEqual((self.stage/'z.xml').read_text(),'edited')
        workflow.copy_files(replace(self.cfg,overwrite_existing=True))
        self.assertEqual((self.stage/'z.xml').read_text(),'new')

    def test_copy_no_matches_and_low_disk(self):
        self.file('skip.bin','x',source=True)
        with self.assertRaisesRegex(ValueError,'No source files'): workflow.copy_files(self.cfg)
        self.file('model.xml','abc',source=True)
        with patch('ats_mda_utils.shutil.disk_usage',return_value=type('Usage',(),{'free':0})()),self.assertRaises(OSError):
            workflow.copy_files(self.cfg)
        self.assertFalse((self.stage/'model.xml').exists())

    def test_atomic_failure_retains_previous_file(self):
        p=self.file('flmd.csv','previous')
        with self.assertRaises(OSError):
            with workflow.atomic_target(p) as temp:
                temp.write_text('partial')
                raise OSError('disk full fixture')
        self.assertEqual(p.read_text(),'previous')
        self.assertFalse(list(self.stage.glob('.ats-mda-*')))

    def test_cleanup_preview_and_explicit_visualization(self):
        checkpoint=self.file('run0/checkpoint0001.h5'); self.file('run0/checkpoint_final.h5')
        vis=self.file('run0/ats_vis_data.h5'); xmf=self.file('run0/output.xmf')
        preview=workflow.cleanup_files(self.cfg)
        self.assertEqual(len(preview),1); self.assertTrue(checkpoint.exists())
        workflow.cleanup_files(replace(self.cfg,cleanup_mode='apply'))
        self.assertFalse(checkpoint.exists()); self.assertTrue(vis.exists()); self.assertTrue(xmf.exists())
        workflow.cleanup_files(replace(self.cfg,cleanup_mode='apply',visualization_run_dirs=('run0',)))
        self.assertFalse(vis.exists()); self.assertFalse(xmf.exists())
        self.assertTrue((self.stage/'run0/checkpoint_final.h5').exists())

    def test_cleanup_missing_final_and_missing_run(self):
        checkpoint=self.file('run0/nested/checkpoint001.h5')
        self.file('run0/checkpoint_final.h5')
        self.assertTrue(workflow.cleanup_files(replace(self.cfg,cleanup_mode='apply')).empty)
        self.assertTrue(checkpoint.exists())
        self.assertTrue(workflow.cleanup_files(replace(self.cfg,run_tokens=('absent',))).empty)

    def test_cleanup_rejects_outside_visualization_directory(self):
        with self.assertRaises(ValueError):
            workflow.cleanup_files(replace(self.cfg,visualization_run_dirs=('../source',),cleanup_mode='apply'))

    def test_staging_symlink_rejected(self):
        outside=self.file('original.xml','source',source=True)
        try: (self.stage/'linked.xml').symlink_to(outside)
        except OSError as exc: self.skipTest(f'Symlink capability unavailable: {exc}')
        with self.assertRaisesRegex(ValueError,'symbolic link'): workflow.generate_flmd(self.cfg)
        self.assertEqual(outside.read_text(),'source')

    def test_staging_hardlink_rejected(self):
        outside=self.file('original.xml','source',source=True)
        try: os.link(outside,self.stage/'linked.xml')
        except OSError as exc: self.skipTest(f'Hardlink capability unavailable: {exc}')
        with self.assertRaisesRegex(ValueError,'hard links'): workflow.generate_flmd(self.cfg)

    def test_metadata_schemas_exact_names_and_positions(self):
        a=self.obs(extra='# comment\n')
        b=self.obs('run1/water_balance.csv',extra='# note\nmetadata\n')
        before={p:p.read_bytes() for p in (a,b)}
        workflow.generate_flmd(self.cfg); workflow.generate_dictionary(self.cfg)
        self.assertEqual(list(self.table('flmd.csv').columns),workflow.FLMD_COLUMNS)
        dd=self.table('dd.csv')
        self.assertEqual(list(dd.columns),workflow.DD_COLUMNS)
        self.assertEqual(dd.column_or_row_name.tolist(),['time [d]','water_flux, outlet [mol/s]'])
        self.assertEqual(dd.unit.tolist(),['d','mol/s'])
        rows=self.table('flmd.csv').set_index('file_name')
        self.assertEqual(rows.loc['./run0/water_balance.csv','header_rows'],'1')
        self.assertEqual(rows.loc['./run1/water_balance.csv','header_rows'],'2')
        for p,content in before.items(): self.assertEqual(p.read_bytes(),content)

    def test_metadata_rerun_preserves_manual_edits(self):
        self.prepare_metadata()
        flmd=self.table('flmd.csv'); flmd.loc[0,'file_description']='Manually reviewed description'
        flmd.to_csv(self.stage/'flmd.csv',index=False)
        dd=self.table('dd.csv'); dd.loc[0,'definition']='My definition'; dd.to_csv(self.stage/'dd.csv',index=False)
        workflow.generate_flmd(self.cfg); workflow.generate_dictionary(self.cfg)
        self.assertEqual(self.table('flmd.csv').loc[0,'file_description'],'Manually reviewed description')
        self.assertEqual(self.table('dd.csv').loc[0,'definition'],'My definition')

    def test_multiple_dictionaries_keep_associations(self):
        self.obs(); self.file('energy.csv','time [d],energy [J]\n0,1\n')
        workflow.generate_flmd(self.cfg)
        for handle,name in [('water_balance','water_dd.csv'),('energy','energy_dd.csv')]:
            workflow.generate_dictionary(replace(self.cfg,obs_file_handle=handle,dd_file_name=name))
        rows=self.table('flmd.csv').set_index('file_name')
        self.assertEqual(rows.loc['./run0/water_balance.csv','data_dictionary_file_name'],'water_dd.csv')
        self.assertEqual(rows.loc['./energy.csv','data_dictionary_file_name'],'energy_dd.csv')

    def test_schema_mismatch_and_existing_dictionary_protected(self):
        self.prepare_metadata(); previous=(self.stage/'dd.csv').read_bytes()
        self.file('run1/water_balance.csv','time [d],flow [m]\n0,1\n')
        with self.assertRaisesRegex(ValueError,'Different observation schema'):
            workflow.generate_dictionary(replace(self.cfg,write_new_csv=True))
        self.assertFalse(list(self.stage.rglob('*_cleaned.csv')))
        self.assertEqual((self.stage/'dd.csv').read_bytes(),previous)
        self.file('energy.csv','time [d],energy [J]\n0,1\n')
        with self.assertRaisesRegex(ValueError,'another schema'):
            workflow.generate_dictionary(replace(self.cfg,obs_file_handle='energy'))

    def test_manual_header_and_no_candidates(self):
        self.file('water_balance.csv','metadata\nstep,value\n0,1\n')
        workflow.generate_flmd(self.cfg)
        with self.assertRaisesRegex(ValueError,'First lines'):
            workflow.generate_dictionary(self.cfg)
        workflow.generate_dictionary(replace(self.cfg,header_line_hint=1))
        self.assertEqual(self.table('dd.csv').unit.tolist(),['N/A','N/A'])
        self.assertIsNone(workflow.generate_dictionary(replace(self.cfg,obs_file_handle='absent')))
        with self.assertRaisesRegex(ValueError,'outside'):
            workflow.generate_dictionary(replace(self.cfg,header_line_hint=99))

    def test_duplicate_comment_and_normalized_collision_errors(self):
        for line in ['x [m],x [m]','# time [d],flow [m]',',flow [m]']:
            with self.assertRaises(ValueError): workflow.parse_header(line)
        with self.assertRaises(ValueError): workflow.normalized_names(['a b [m]','a_b [m]'])

    def test_streaming_rewrite_preserves_data_and_original(self):
        p=self.obs(); before=p.read_bytes()
        workflow.generate_flmd(self.cfg)
        cfg=replace(self.cfg,write_new_csv=True)
        workflow.generate_dictionary(cfg)
        target=p.with_name('water_balance_cleaned.csv')
        self.assertEqual(target.read_bytes().split(b'\n',1)[1],before.split(b'\n',1)[1])
        self.assertEqual(p.read_bytes(),before)
        self.assertEqual(self.table('dd.csv').column_or_row_name.tolist(),['time','water_flux_outlet'])
        with self.assertRaises(FileExistsError): workflow.generate_dictionary(cfg)

    def test_offline_lookup_does_not_erase_edits(self):
        self.prepare_metadata()
        dd=self.table('dd.csv'); dd.loc[0,'definition']='Reviewed'; dd.to_csv(self.stage/'dd.csv',index=False)
        with patch('ats_mda_utils.urllib.request.urlopen',side_effect=OSError('offline')) as mock:
            workflow.enrich_dictionary(replace(self.cfg,download_definitions=True))
        self.assertEqual(mock.call_count,2)
        self.assertEqual(self.table('dd.csv').loc[0,'definition'],'Reviewed')

    def test_lookup_only_fills_blank_definitions(self):
        self.prepare_metadata()
        dd=self.table('dd.csv'); dd.loc[0,'definition']='Reviewed'; dd.to_csv(self.stage/'dd.csv',index=False)
        table=b'| Variable Root Name | Description |\n|---+---|\n| time | Time |\n| water_flux | Water flux |\n'
        with patch('ats_mda_utils.urllib.request.urlopen',return_value=io.BytesIO(table)):
            workflow.enrich_dictionary(replace(self.cfg,download_definitions=True))
        self.assertEqual(self.table('dd.csv').definition.tolist(),['Reviewed','Water flux'])

    def test_review_flags_missing_dictionary_and_definition(self):
        self.prepare_metadata()
        self.file('extra.csv','a,b\n1,2\n')
        report=workflow.review_package(self.cfg)
        self.assertTrue(report.needs_review.str.contains('Not in FLMD').any())
        self.assertTrue(report.needs_review.str.contains('Missing definition').any())

    def test_packaging_split_and_checksums(self):
        self.prepare_metadata(); self.file('run0/random.h5',os.urandom(2048).hex())
        workflow.generate_flmd(self.cfg)
        cfg=replace(self.cfg,create_archives=True,split_size_gib=512/1024**3)
        summary=workflow.package_archives(cfg)
        output=workflow.archive_directory(cfg)
        archive=output/'run0.tar.gz'
        parts=sorted(output.glob('run0.tar.gz.part*'))
        self.assertGreater(len(parts),1)
        self.assertEqual(b''.join(p.read_bytes() for p in parts),archive.read_bytes())
        with tarfile.open(archive) as tar:
            self.assertIn('run0/water_balance.csv',tar.getnames())
        self.assertTrue((output/'flmd.csv').exists()); self.assertTrue((output/'dd.csv').exists())
        self.assertEqual(len(summary),1)
        for manifest in workflow.create_checksums(cfg):
            for line in manifest.read_text().splitlines():
                checksum,name=line.split('  ',1)
                self.assertEqual(hashlib.sha256((manifest.parent/name).read_bytes()).hexdigest(),checksum)
        self.assertTrue(self.stage.exists())

    def test_incomplete_and_stale_archives_rejected(self):
        self.prepare_metadata()
        cfg=replace(self.cfg,create_archives=True)
        output=workflow.archive_directory(cfg); output.mkdir(); (output/'partial.tar.gz').write_bytes(b'bad')
        with self.assertRaises(FileExistsError): workflow.package_archives(cfg)
        with self.assertRaisesRegex(RuntimeError,'did not complete'): workflow.create_checksums(cfg)
        cfg=replace(cfg,archive_output_dir=self.root/'fresh')
        workflow.package_archives(cfg)
        self.file('new.txt','changed')
        with self.assertRaisesRegex(RuntimeError,'changed after packaging'): workflow.create_checksums(cfg)

    def test_packaging_disabled_root_only_and_output_overlap(self):
        self.assertTrue(workflow.package_archives(self.cfg).empty)
        self.file('model.xml','xml'); workflow.generate_flmd(self.cfg)
        cfg=replace(self.cfg,create_archives=True)
        self.assertTrue(workflow.package_archives(cfg).empty)
        self.assertTrue((workflow.archive_directory(cfg)/'model.xml').exists())
        with self.assertRaises(ValueError):
            workflow.archive_directory(replace(cfg,archive_output_dir=self.stage/'archives'))

    def test_packaging_root_name_collision_rejected(self):
        self.prepare_metadata()
        self.file('run0.tar.gz','existing data')
        cfg=replace(self.cfg,create_archives=True)
        with self.assertRaisesRegex(ValueError,'collides'):
            workflow.package_archives(cfg)
        self.assertEqual((self.stage/'run0.tar.gz').read_text(),'existing data')

    def test_failed_hash_retains_previous_manifest(self):
        self.file('data.csv','x')
        manifest=self.file('sha256sums.txt','previous')
        with patch('ats_mda_utils.digest',side_effect=OSError('read failed')),self.assertRaises(OSError):
            workflow.create_checksums(self.cfg)
        self.assertEqual(manifest.read_text(),'previous')

    def test_logging_visible_and_not_duplicated(self):
        workflow.configure_logging(self.cfg,stream=self.log)
        workflow.configure_logging(self.cfg,stream=self.log)
        workflow.logger.info('unique milestone')
        with workflow.step('test step'):
            progress=workflow.Progress('test progress',2,1,'files')
            progress.advance(2); progress.report(force=True)
        self.assertEqual(self.log.getvalue().count('unique milestone'),1)
        for text in ['START','DONE','100.0%']:
            self.assertIn(text,self.log.getvalue())

    def test_all_notebook_cells_on_synthetic_package(self):
        self.file('run0/water_balance.csv','time [d],flow [m]\n0,1\n',source=True)
        notebook=json.loads((Path(workflow.__file__).parent/'ats_mda_workflow.ipynb').read_text())
        namespace={}
        for i,cell in enumerate(notebook['cells']):
            if cell['cell_type']!='code': continue
            if i==3:
                namespace['cfg']=replace(self.cfg,create_archives=True)
                workflow.configure_logging(namespace['cfg'],stream=self.log)
            else:
                exec(compile(''.join(cell['source']),f'cell-{i}','exec'),namespace)
        self.assertTrue((self.stage/'flmd.csv').exists())
        self.assertTrue((self.stage/'dd.csv').exists())
        self.assertTrue((self.stage/'sha256sums.txt').exists())


if __name__=='__main__':
    unittest.main()
