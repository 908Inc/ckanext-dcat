import json
import uuid
import logging
import hashlib
import traceback
import re

from urlparse import urljoin

from datapackage import Resource

import ckan.plugins as p
import ckan.model as model

import ckan.lib.plugins as lib_plugins

from ckan.common import config, c

from ckanext.harvest.model import HarvestObject, HarvestObjectExtra

from ckanext.dcat.harvesters.base import DCATHarvester

from ckanext.dcat.processors import RDFParserException, RDFParser

from ckanext.dcat.interfaces import IDCATRDFHarvester

from ckanext.dcat.models import DCATPackageExtra

import ckan.plugins.toolkit as toolkit

from ckanext.dgua.plugin import (
    UnicodePackage,
    remove_datapackage_files,
    get_datapackage_full_path,
    NotFound,
    DatapackageValidationError
)

import ckanext.dgua.helpers as helpers
import ckanext.dgua.models as dgua_model


log = logging.getLogger(__name__)


def datapackage_generate(pkg):
    context = {
        'model': model,
        'session': model.Session,
        'user': c.user,
        'auth_user_obj': c.userobj,
    }
    pkg_descriptor = {
        'profile': 'data-package',
        'id': pkg.get('id'),
        'name': pkg.get('name'),
        'title': pkg.get('title', ''),
        'description': pkg.get('notes', ''),
        'contributors': [{
            'title': pkg.get('author', ''),
            'email': pkg.get('author_email', ''),
            'role': 'author'
        }, {
            'title': pkg.get('maintainer', ''),
            'email': pkg.get('maintainer_email', ''),
            'role': 'maintainer'
        }],
        'homepage': urljoin(toolkit.config['ckan.site_url'],
                            toolkit.url_for(controller='package', action='read', id=pkg.get('name'))),
        'version': pkg.get('version'),
        'created': pkg.get('metadata_created')
    }
    if pkg.get('num_tags') > 0:
        pkg_descriptor.update({
            'keywords': [i.get('name') for i in pkg.get('tags')],
        })
    if 'license_title' in pkg and 'license_id' in pkg:
        license_dict = {
            'name': pkg.get('license_id'),
            'title': pkg.gte('license_title'),
        }
        if 'license_url' in pkg:
            license_dict.update({'path': pkg.gtet('license_url')})
        pkg_descriptor.update({'licenses': [license_dict]})
    package = UnicodePackage(pkg_descriptor)
    full_file_path = get_datapackage_full_path(pkg)
    helpers.ensure_dir(full_file_path)
    datastore_info = toolkit.get_action('datastore_info')
    for res_dict in pkg.get('resources'):
        descriptor = {
            'name': res_dict.get('name'),
            'mimetype': res_dict.get('mimetype'),
            'format': res_dict.get('format'),
            'url': res_dict.get('url'),
        }
        try:
            info = datastore_info(context, {'id': res_dict.get('id')})
        except Exception:
            pass
        else:
            fields = [{'name': f_name, 'type': f_type, 'format': 'default'} for f_name, f_type in
                      info['schema'].items()]
            descriptor.update({
                'profile': 'tabular-data-resource',
                'schema': {'fields': fields, 'missingValues': ['']},
                'url': urljoin(toolkit.config['ckan.site_url'], '/datastore/dump/{}'.format(res_dict.get('id'))),
                'format': 'CSV',
                'mimetype': 'text/csv',
            })
        resource = Resource(descriptor)
        package.add_resource(resource.descriptor)
    try:
        package.validate()
    except DatapackageValidationError:
        log.warning(package.errors)
    package.save(full_file_path)
    pkg['is_datapackage'] = True
    toolkit.get_action('package_update')(context, pkg)

class DCATRDFHarvester(DCATHarvester):

    def info(self):
        return {
            'name': 'dcat_rdf',
            'title': 'Generic DCAT RDF Harvester',
            'description': 'Harvester for DCAT datasets from an RDF graph'
        }

    _names_taken = []

    def _get_dict_value(self, _dict, key, default=None):
        '''
        Returns the value for the given key on a CKAN dict

        By default a key on the root level is checked. If not found, extras
        are checked, both with the key provided and with `dcat_` prepended to
        support legacy fields.

        If not found, returns the default value, which defaults to None
        '''

        if key in _dict:
            return _dict[key]

        for extra in _dict.get('extras', []):
            if extra['key'] == key or extra['key'] == 'dcat_' + key:
                return extra['value']

        return default

    def _get_guid(self, dataset_dict, source_url=None):
        '''
        Try to get a unique identifier for a harvested dataset

        It will be the first found of:
         * URI (rdf:about)
         * dcat:identifier
         * Source URL + Dataset name
         * Dataset name

         The last two are obviously not optimal, as depend on title, which
         might change.

         Returns None if no guid could be decided.
        '''
        guid = None

        guid = (
            self._get_dict_value(dataset_dict, 'uri') or
            self._get_dict_value(dataset_dict, 'identifier')
        )
        if guid:
            return guid

        if dataset_dict.get('name'):
            guid = dataset_dict['name']
            if source_url:
                guid = source_url.rstrip('/') + '/' + guid
        return guid

    def _mark_datasets_for_deletion(self, guids_in_source, harvest_job):
        '''
        Given a list of guids in the remote source, checks which in the DB
        need to be deleted

        To do so it queries all guids in the DB for this source and calculates
        the difference.

        For each of these creates a HarvestObject with the dataset id, marked
        for deletion.

        Returns a list with the ids of the Harvest Objects to delete.
        '''

        object_ids = []

        # Get all previous current guids and dataset ids for this source
        query = model.Session.query(HarvestObject.guid, HarvestObject.package_id) \
                             .filter(HarvestObject.current==True) \
                             .filter(HarvestObject.harvest_source_id==harvest_job.source.id)

        guid_to_package_id = {}
        for guid, package_id in query:
            guid_to_package_id[guid] = package_id

        guids_in_db = guid_to_package_id.keys()

        # Get objects/datasets to delete (ie in the DB but not in the source)
        guids_to_delete = set(guids_in_db) - set(guids_in_source)

        # Create a harvest object for each of them, flagged for deletion
        for guid in guids_to_delete:
            obj = HarvestObject(guid=guid, job=harvest_job,
                                package_id=guid_to_package_id[guid],
                                extras=[HarvestObjectExtra(key='status',
                                                           value='delete')])

            # Mark the rest of objects for this guid as not current
            model.Session.query(HarvestObject) \
                         .filter_by(guid=guid) \
                         .update({'current': False}, False)
            obj.save()
            object_ids.append(obj.id)

        return object_ids

    def validate_config(self, source_config):
        if not source_config:
            return source_config

        source_config_obj = json.loads(source_config)
        if 'rdf_format' in source_config_obj:
            rdf_format = source_config_obj['rdf_format']
            if not isinstance(rdf_format, basestring):
                raise ValueError('rdf_format must be a string')
            supported_formats = RDFParser().supported_formats()
            if rdf_format not in supported_formats:
                raise ValueError('rdf_format should be one of: ' + ", ".join(supported_formats))

        return source_config

    def gather_stage(self, harvest_job):

        log.debug('In DCATRDFHarvester gather_stage')

        rdf_format = None
        if harvest_job.source.config:
            rdf_format = json.loads(harvest_job.source.config).get("rdf_format")

        # Get file contents of first page
        next_page_url = harvest_job.source.url

        guids_in_source = []
        object_ids = []
        last_content_hash = None
        self._names_taken = []

        while next_page_url:
            for harvester in p.PluginImplementations(IDCATRDFHarvester):
                next_page_url, before_download_errors = harvester.before_download(next_page_url, harvest_job)

                for error_msg in before_download_errors:
                    self._save_gather_error(error_msg, harvest_job)

                if not next_page_url:
                    return []

            content, rdf_format = self._get_content_and_type(next_page_url, harvest_job, 1, content_type=rdf_format)

            content_hash = hashlib.md5()
            if content:
                content_hash.update(content)

            if last_content_hash:
                if content_hash.digest() == last_content_hash.digest():
                    log.warning('Remote content was the same even when using a paginated URL, skipping')
                    break
            else:
                last_content_hash = content_hash

            # TODO: store content?
            for harvester in p.PluginImplementations(IDCATRDFHarvester):
                content, after_download_errors = harvester.after_download(content, harvest_job)

                for error_msg in after_download_errors:
                    self._save_gather_error(error_msg, harvest_job)

            if not content:
                return []

            # TODO: profiles conf
            parser = RDFParser()

            try:
                parser.parse(content, _format=rdf_format)
            except RDFParserException, e:
                self._save_gather_error('Error parsing the RDF file: {0}'.format(e), harvest_job)
                return []

            try:

                source_dataset = model.Package.get(harvest_job.source.id)

                for dataset in parser.datasets():
                    if not dataset.get('name'):
                        dataset['name'] = self._gen_new_name(dataset['title'])
                    if dataset['name'] in self._names_taken:
                        suffix = len([i for i in self._names_taken if i.startswith(dataset['name'] + '-')]) + 1
                        dataset['name'] = '{}-{}'.format(dataset['name'], suffix)
                    self._names_taken.append(dataset['name'])

                    # Unless already set by the parser, get the owner organization (if any)
                    # from the harvest source dataset
                    if not dataset.get('owner_org'):
                        if source_dataset.owner_org:
                            dataset['owner_org'] = source_dataset.owner_org

                    # Try to get a unique identifier for the harvested dataset
                    guid = self._get_guid(dataset, source_url=source_dataset.url)

                    if not guid:
                        self._save_gather_error('Could not get a unique identifier for dataset: {0}'.format(dataset),
                                                harvest_job)
                        continue

                    dataset['extras'].append({'key': 'guid', 'value': guid})
                    guids_in_source.append(guid)

                    obj = HarvestObject(guid=guid, job=harvest_job,
                                        content=json.dumps(dataset))

                    obj.save()
                    object_ids.append(obj.id)
            except Exception, e:
                self._save_gather_error('Error when processsing dataset: %r / %s' % (e, traceback.format_exc()),
                                        harvest_job)
                return []

            # get the next page
            next_page_url = parser.next_page()

        # Check if some datasets need to be deleted
        object_ids_to_delete = self._mark_datasets_for_deletion(guids_in_source, harvest_job)

        object_ids.extend(object_ids_to_delete)

        return object_ids

    def fetch_stage(self, harvest_object):
        # Nothing to do here
        return True

    def _fix_package_schema(self, dataset):
        package_plugin = lib_plugins.lookup_package_plugin(dataset.get('type', None))

        package_schema = package_plugin.create_package_schema()

        # Deleting extra keys from extras if extras keys in schema
        if set([el.get('key') for el in dataset.get('extras', [])]).intersection(set(package_schema.keys())):
            for schema_key in package_schema.keys():
                for extra_key in dataset['extras']:
                    if extra_key.get('key') == schema_key:
                        dataset[extra_key.get('key')] = extra_key.get('value')
                        dataset['extras'].remove(extra_key)
        return dataset

    def import_stage(self, harvest_object):

        log.debug('In DCATRDFHarvester import_stage')

        status = self._get_object_extra(harvest_object, 'status')
        if status == 'delete':
            # Delete package
            context = {'model': model, 'session': model.Session,
                       'user': self._get_user_name(), 'ignore_auth': True}

            p.toolkit.get_action('package_delete')(context, {'id': harvest_object.package_id})
            log.info('Deleted package {0} with guid {1}'.format(harvest_object.package_id,
                                                                harvest_object.guid))
            return True

        if harvest_object.content is None:
            self._save_object_error('Empty content for object {0}'.format(harvest_object.id),
                                    harvest_object, 'Import')
            return False

        try:

            dataset = json.loads(harvest_object.content)

            # Set default values
            required_fields = DCATPackageExtra.get_extra_keys(harvest_object.harvest_source_id)
            if not dataset.get('purpose_of_collecting_information'):
                dataset['purpose_of_collecting_information'] = required_fields['purpose_of_collecting_information']
            if not dataset.get('update_frequency'):
                dataset['update_frequency'] = required_fields['update_frequency']
            if not dataset.get('language'):
                dataset['language'] = required_fields['language']
            if not dataset.get('notes'):
                dataset['notes'] = dataset.get('title')

            tagname_match = re.compile('[\w \-.]*$', re.UNICODE)
            dataset['tags'] = [tag for tag in dataset.get('tags') if tagname_match.match(tag.get('name'))]

            if not dataset.get('tag_string'):
                if dataset['tags']:
                    dataset['tag_string'] = u', '.join([tag.get('name') for tag in dataset['tags']])
                else:
                    dataset['tag_string'] = required_fields.get('tag_string')

            dataset['private'] = True

        except ValueError:
            self._save_object_error('Could not parse content for object {0}'.format(harvest_object.id),
                                    harvest_object, 'Import')
            return False

        # Get the last harvested object (if any)
        previous_object = model.Session.query(HarvestObject) \
                                       .filter(HarvestObject.guid==harvest_object.guid) \
                                       .filter(HarvestObject.current==True) \
                                       .first()

        # Flag previous object as not current anymore
        if previous_object:
            previous_object.current = False
            previous_object.add()

        # Flag this object as the current one
        harvest_object.current = True
        harvest_object.add()

        context = {
            'user': self._get_user_name(),
            'return_id_only': True,
            'ignore_auth': True,
        }

        dataset = self.modify_package_dict(dataset, {}, harvest_object)

        # Check if a dataset with the same guid exists
        existing_dataset = self._get_existing_dataset(harvest_object.guid)

        try:
            if existing_dataset:
                # Don't change the dataset name even if the title has
                dataset['name'] = existing_dataset['name']
                dataset['id'] = existing_dataset['id']

                dataset = self._fix_package_schema(dataset)

                # Set package as public if it exists and was be approved by moderator
                premoderation_list = dgua_model.DGUAPremoderationLog.get_by_pkg_id(dataset['id'])

                premoderation_pkg = None

                if premoderation_list:
                    premoderation_pkg = premoderation_list[0]

                if premoderation_pkg and premoderation_pkg.action == 'approved':
                    dataset['private'] = False

                harvester_tmp_dict = {}

                # check if resources already exist based on their URI
                existing_resources =  existing_dataset.get('resources')
                resource_mapping = {r.get('uri'): r.get('id') for r in existing_resources if r.get('uri')}
                for resource in dataset.get('resources'):
                    res_uri = resource.get('uri')
                    if res_uri and res_uri in resource_mapping:
                        resource['id'] = resource_mapping[res_uri]

                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    harvester.before_update(harvest_object, dataset, harvester_tmp_dict)

                try:
                    if dataset:
                        # Save reference to the package on the object
                        harvest_object.package_id = dataset['id']
                        harvest_object.add()

                        p.toolkit.get_action('package_update')(context, dataset)
                    else:
                        log.info('Ignoring dataset %s' % existing_dataset['name'])
                        return 'unchanged'
                except p.toolkit.ValidationError, e:
                    self._save_object_error('Update validation Error: %s' % str(e.error_summary), harvest_object, 'Import')
                    return False

                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    err = harvester.after_update(harvest_object, dataset, harvester_tmp_dict)

                    if err:
                        self._save_object_error('RDFHarvester plugin error: %s' % err, harvest_object, 'Import')
                        return False

                log.info('Updated dataset %s' % dataset['name'])

            else:

                dataset = self._fix_package_schema(dataset)
                package_plugin = lib_plugins.lookup_package_plugin(dataset.get('type', None))
                #
                package_schema = package_plugin.create_package_schema()
                #
                # Deleting extra keys from extras if extras keys in schema
                # if set([el.get('key') for el in dataset.get('extras', [])]).intersection(set(package_schema.keys())):
                #     for schema_key in package_schema.keys():
                #         for extra_key in dataset['extras']:
                #             if extra_key.get('key') == schema_key:
                #                 dataset[extra_key.get('key')] = extra_key.get('value')
                #                 dataset['extras'].remove(extra_key)

                context['schema'] = package_schema

                # We need to explicitly provide a package ID
                dataset['id'] = unicode(uuid.uuid4())
                package_schema['id'] = [unicode]

                harvester_tmp_dict = {}

                name = dataset['name']
                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    harvester.before_create(harvest_object, dataset, harvester_tmp_dict)

                try:
                    if dataset:
                        # Save reference to the package on the object
                        harvest_object.package_id = dataset['id']
                        harvest_object.add()

                        # Defer constraints and flush so the dataset can be indexed with
                        # the harvest object id (on the after_show hook from the harvester
                        # plugin)
                        model.Session.execute('SET CONSTRAINTS harvest_object_package_id_fkey DEFERRED')
                        model.Session.flush()

                        p.toolkit.get_action('package_create')(context, dataset)
                    else:
                        log.info('Ignoring dataset %s' % name)
                        return 'unchanged'
                except p.toolkit.ValidationError, e:
                    self._save_object_error('Create validation Error: %s' % str(e.error_summary), harvest_object, 'Import')
                    return False

                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    err = harvester.after_create(harvest_object, dataset, harvester_tmp_dict)

                    if err:
                        self._save_object_error('RDFHarvester plugin error: %s' % err, harvest_object, 'Import')
                        return False

                datapackage_generate(dataset)
                log.info('Created dataset %s' % dataset['name'])

        except Exception, e:
            self._save_object_error('Error importing dataset %s: %r / %s' % (dataset.get('name', ''), e, traceback.format_exc()), harvest_object, 'Import')
            return False

        finally:
            model.Session.commit()

        return True
