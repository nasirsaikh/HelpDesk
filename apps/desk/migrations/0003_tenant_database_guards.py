from django.db import migrations
from apps.tenancy.db_guards import install_guards, remove_guards

class Migration(migrations.Migration):
    dependencies = [("desk", "0002_initial")]
    operations = [migrations.RunPython(install_guards, remove_guards)]
