# -*- coding:utf-8 -*-

import os.path
import jinja2
from copy import copy

from statik.config import StatikConfig
from statik.utils import *
from statik.errors import *
from statik.models import StatikModel
from statik.views import StatikView
from statik.jinja2ext import *
from statik.database import StatikDatabase

import logging
logger = logging.getLogger(__name__)

__all__ = [
    'StatikProject',
]


class StatikProject(object):

    VIEWS_DIR = "views"
    MODELS_DIR = "models"
    TEMPLATES_DIR = "templates"
    DATA_DIR = "data"

    def __init__(self, path, **kwargs):
        """Constructor.

        Args:
            path: The full filesystem path to the base of the project.
        """
        self.path = path
        logger.info("Using project source directory: %s" % path)
        self.config = kwargs.get('config', None)
        self.models = {}
        self.template_env = None
        self.views = {}
        self.db = None
        self.project_context = {}

    def generate(self, output_path=None, in_memory=False):
        """Executes the Statik project generator."""
        if output_path is None and not in_memory:
            raise ValueError("If project is not to be generated in-memory, an output path must be specified")

        self.config = self.config or StatikConfig(os.path.join(self.path, 'config.yml'))
        self.models = self.load_models()
        self.template_env = self.configure_templates()

        self.views = self.load_views()
        if len(self.views) == 0:
            raise NoViewsError("Project has no views configured")

        self.template_env.statik_views = self.views
        self.template_env.statik_base_url = self.config.base_path
        self.template_env.statik_base_asset_url = add_url_path_component(
                self.config.base_path,
                self.config.assets_dest_path
        )
        self.db = self.load_db_data(self.models)
        self.project_context = self.load_project_context()

        in_memory_result = self.process_views()

        if in_memory:
            return in_memory_result
        else:
            # dump the in-memory output to files
            file_count = self.dump_in_memory_result(in_memory_result, output_path)
            logger.info('Wrote %d output file(s) to folder: %s' % (file_count, output_path))
            # copy any assets across, recursively
            self.copy_assets(output_path)
            return file_count

    def configure_templates(self):
        template_path = os.path.join(self.path, StatikProject.TEMPLATES_DIR)
        if not os.path.isdir(template_path):
            raise MissingProjectFolderError(StatikProject.TEMPLATES_DIR, "Project is missing its templates folder")

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_path),
            extensions=[
                'statik.jinja2ext.StatikUrlExtension',
                'statik.jinja2ext.StatikAssetExtension',
            ]
        )
        env.filters['date'] = filter_datetime
        return env

    def load_models(self):
        models_path = os.path.join(self.path, StatikProject.MODELS_DIR)
        logger.debug("Loading models from: %s" % models_path)
        if not os.path.isdir(models_path):
            raise MissingProjectFolderError(StatikProject.MODELS_DIR, "Project is missing its models folder")

        model_files = list_files(models_path, ['yml', 'yaml'])
        logger.debug("Found %d model(s) in project" % len(model_files))
        # get all of the models' names
        model_names = [extract_filename(model_file) for model_file in model_files]
        models = {}
        for model_file in model_files:
            model_name = extract_filename(model_file)
            models[model_name] = StatikModel(
                os.path.join(models_path, model_file),
                name=model_name,
                model_names=model_names
            )

        return models

    def load_views(self):
        """Loads the views for this project from the project directory
        structure."""
        view_path = os.path.join(self.path, StatikProject.VIEWS_DIR)
        logger.debug("Loading views from: %s" % view_path)
        if not os.path.isdir(view_path):
            raise MissingProjectFolderError(StatikProject.VIEWS_DIR, "Project is missing its views folder")

        view_files = list_files(view_path, ['yml', 'yaml'])
        logger.debug("Found %d view(s) in project" % len(view_files))
        views = {}
        for view_file in view_files:
            view_name = extract_filename(view_file)
            views[view_name] = StatikView(
                os.path.join(view_path, view_file),
                name=view_name,
                models=self.models,
                template_env=self.template_env,
            )

        return views

    def load_db_data(self, models):
        data_path = os.path.join(self.path, StatikProject.DATA_DIR)
        logger.debug("Loading data from: %s" % data_path)
        if not os.path.isdir(data_path):
            raise MissingProjectFolderError(StatikProject.DATA_DIR, "Project is missing its data folder")

        return StatikDatabase(data_path, models)

    def load_project_context(self):
        """Loads the project context (static and dynamic) from the database/models for common use amongst
        the project's views."""
        # just make a copy of the project context
        context = copy(self.config.context_static)
        context['project_name'] = self.config.project_name
        context['base_path'] = self.config.base_path

        # now load the dynamic context
        context.update(self.load_project_dynamic_context())
        return context

    def load_project_dynamic_context(self):
        """Loads the dynamic context for this project, if any."""
        context = {}
        for varname, query in self.config.context_dynamic.items():
            context[varname] = self.db.query(query)
        return context

    def process_views(self):
        """Processes the loaded views to generate the required output data."""
        output = {}
        logger.debug("Processing %d view(s)..." % len(self.views))
        for view_name, view in self.views.items():
            # first update the view's context with the project context
            view.context.update(self.project_context)
            output.update(view.process(self.db))
        return output

    def dump_in_memory_result(self, result, output_path):
        """Recursively dumps the result of our processing into files within the
        given output path.

        Args:
            result: The in-memory result of our processing.
            output_path: Full path to the folder into which to dump the files.

        Returns:
            The number of files generated (integer).
        """
        file_count = 0
        logger.debug("Dumping in-memory processing results to output folder: %s" % output_path)
        for k, v in result.items():
            cur_output_path = os.path.join(output_path, k)

            if isinstance(v, dict):
                file_count += self.dump_in_memory_result(v, cur_output_path)
            else:
                if not os.path.isdir(output_path):
                    os.makedirs(output_path)

                filename = os.path.join(output_path, k)
                logger.info("Writing output file: %s" % filename)
                # dump the contents of the file
                with open(filename, 'wt') as f:
                    f.write(v)

                file_count += 1

        return file_count

    def copy_assets(self, output_path):
        """Copies all asset files from the source path to the destination
        path. If no such source path exists, no asset copying will be performed.
        """
        src_path = self.config.assets_src_path
        if not os.path.isabs(src_path):
            src_path = os.path.join(self.path, src_path)

        if os.path.isdir(src_path):
            dest_path = self.config.assets_dest_path
            if not os.path.isabs(dest_path):
                dest_path = os.path.join(output_path, dest_path)

            logger.info("Copying assets from %s to %s..." % (src_path, dest_path))
            asset_count = copy_tree(src_path, dest_path)
            logger.info("Copied %s asset(s)" % asset_count)
        else:
            logger.info("Missing assets source path - skipping copying of assets: %s" % src_path)
