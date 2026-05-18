from django.core.management.commands.migrate import Command as DjangoMigrateCommand
from django.db.migrations.operations.fields import AddField
from django.db.migrations.operations.models import AddConstraint, AddIndex, CreateModel


class Command(DjangoMigrateCommand):
    help = (
        "Run migrations with fake-initial enabled by default and skip create/add "
        "operations that target schema objects already present in the database."
    )

    _patched = False

    @staticmethod
    def _table_exists(schema_editor, table_name):
        with schema_editor.connection.cursor() as cursor:
            return table_name in schema_editor.connection.introspection.table_names(cursor)

    @staticmethod
    def _column_exists(schema_editor, table_name, column_name):
        with schema_editor.connection.cursor() as cursor:
            for column in schema_editor.connection.introspection.get_table_description(cursor, table_name):
                if getattr(column, "name", "").lower() == column_name.lower():
                    return True
        return False

    @staticmethod
    def _constraint_exists(schema_editor, table_name, constraint_name):
        with schema_editor.connection.cursor() as cursor:
            constraints = schema_editor.connection.introspection.get_constraints(cursor, table_name)
        return constraint_name in constraints

    @classmethod
    def _patch_migration_operations(cls):
        if cls._patched:
            return

        original_create_model = CreateModel.database_forwards
        original_add_field = AddField.database_forwards
        original_add_constraint = AddConstraint.database_forwards
        original_add_index = AddIndex.database_forwards

        def create_model_database_forwards(self, app_label, schema_editor, from_state, to_state):
            model = to_state.apps.get_model(app_label, self.name)
            if cls._table_exists(schema_editor, model._meta.db_table):
                return
            return original_create_model(self, app_label, schema_editor, from_state, to_state)

        def add_field_database_forwards(self, app_label, schema_editor, from_state, to_state):
            model = to_state.apps.get_model(app_label, self.model_name)
            field = model._meta.get_field(self.name)
            if cls._table_exists(schema_editor, model._meta.db_table) and cls._column_exists(
                schema_editor,
                model._meta.db_table,
                field.column,
            ):
                return
            return original_add_field(self, app_label, schema_editor, from_state, to_state)

        def add_constraint_database_forwards(self, app_label, schema_editor, from_state, to_state):
            model = to_state.apps.get_model(app_label, self.model_name)
            if cls._table_exists(schema_editor, model._meta.db_table) and cls._constraint_exists(
                schema_editor,
                model._meta.db_table,
                self.constraint.name,
            ):
                return
            return original_add_constraint(self, app_label, schema_editor, from_state, to_state)

        def add_index_database_forwards(self, app_label, schema_editor, from_state, to_state):
            model = to_state.apps.get_model(app_label, self.model_name)
            if cls._table_exists(schema_editor, model._meta.db_table) and cls._constraint_exists(
                schema_editor,
                model._meta.db_table,
                self.index.name,
            ):
                return
            return original_add_index(self, app_label, schema_editor, from_state, to_state)

        CreateModel.database_forwards = create_model_database_forwards
        AddField.database_forwards = add_field_database_forwards
        AddConstraint.database_forwards = add_constraint_database_forwards
        AddIndex.database_forwards = add_index_database_forwards
        cls._patched = True

    def handle(self, *args, **options):
        self._patch_migration_operations()
        options["fake_initial"] = True
        return super().handle(*args, **options)
