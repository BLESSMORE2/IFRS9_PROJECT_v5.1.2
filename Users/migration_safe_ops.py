from django.db import migrations


def _table_exists(schema_editor, table_name):
    with schema_editor.connection.cursor() as cursor:
        return table_name in schema_editor.connection.introspection.table_names(cursor)


def _column_exists(schema_editor, table_name, column_name):
    with schema_editor.connection.cursor() as cursor:
        for column in schema_editor.connection.introspection.get_table_description(cursor, table_name):
            if getattr(column, "name", "").lower() == column_name.lower():
                return True
    return False


class CreateModelIfMissing(migrations.CreateModel):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        if _table_exists(schema_editor, model._meta.db_table):
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class AddFieldIfMissing(migrations.AddField):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        field = model._meta.get_field(self.name)
        if _table_exists(schema_editor, model._meta.db_table) and _column_exists(schema_editor, model._meta.db_table, field.column):
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)