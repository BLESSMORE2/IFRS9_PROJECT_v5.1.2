from django.core.management.base import BaseCommand

from scorecard.models import IFRS9ScoreSheetTemplate


class Command(BaseCommand):
    help = "Update IFRS9 templates to 'approved' status so they appear in the template list."

    def add_arguments(self, parser):
        parser.add_argument(
            '--code',
            type=str,
            help='Specific template code to update (e.g., IFRS9-PD-001). If not provided, updates all templates.',
        )

    def handle(self, *args, **options):
        template_code = options.get('code')
        
        if template_code:
            templates = IFRS9ScoreSheetTemplate.objects.filter(code=template_code)
        else:
            templates = IFRS9ScoreSheetTemplate.objects.all()
        
        updated_count = 0
        for template in templates:
            if template.status != 'approved':
                old_status = template.status
                template.status = 'approved'
                template.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"✓ Updated {template.code} ({template.name}): "
                        f"{old_status} → approved"
                    )
                )
                updated_count += 1
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"⊘ {template.code} ({template.name}) already has status 'approved'"
                    )
                )
        
        if updated_count == 0:
            self.stdout.write(
                self.style.SUCCESS("No templates needed updating. All templates are already approved.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"\n✅ Successfully updated {updated_count} template(s) to 'approved' status.")
            )
