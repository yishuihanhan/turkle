import datetime
import os.path
import re
import sys

from bs4 import BeautifulSoup
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from jsonfield import JSONField
import unicodecsv


# The default field size limit is 131072 characters
unicodecsv.field_size_limit(sys.maxsize)


class Hit(models.Model):
    """
    Human Intelligence Task
    """
    class Meta:
        verbose_name = "HIT"

    hit_batch = models.ForeignKey('HitBatch')
    completed = models.BooleanField(default=False)
    input_csv_fields = JSONField()

    def __unicode__(self):
        return 'HIT id:{}'.format(self.id)

    def __str__(self):
        return 'HIT id:{}'.format(self.id)

    def generate_form(self):
        result = self.hit_batch.hit_template.form
        for field in self.input_csv_fields.keys():
            result = result.replace(
                r'${' + field + r'}',
                self.input_csv_fields[field]
            )

        # Surround the html in the form with two div tags:
        # one surrounding the HIT in a black box
        # and the other creating some white space between the black box and the
        # form.
        border = (
            '<div style="'
            ' width:100%%;'
            ' border:2px solid black;'
            ' margin-top:10px'
            '">'
            '%s'
            '</div>'
        )
        margin = '<div style="margin:10px">%s</div>'

        result = margin % result
        result = border % result
        return result


class HitAssignment(models.Model):
    class Meta:
        verbose_name = "HIT Assignment"

    answers = JSONField(blank=True)
    assigned_to = models.ForeignKey(User, null=True)
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True)
    hit = models.ForeignKey(Hit)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.expires_at = timezone.now() + \
            datetime.timedelta(hours=self.hit.hit_batch.allotted_assignment_time)

        if 'csrfmiddlewaretoken' in self.answers:
            del self.answers['csrfmiddlewaretoken']
        super(HitAssignment, self).save(*args, **kwargs)

        # Mark HIT as completed if all Assignments have been completed
        if self.hit.hitassignment_set.filter(completed=True).count() >= \
           self.hit.hit_batch.assignments_per_hit:
            self.hit.completed = True
            self.hit.save()


class HitBatch(models.Model):
    class Meta:
        verbose_name = "HIT batch"
        verbose_name_plural = "HIT batches"

    active = models.BooleanField(default=True)
    allotted_assignment_time = models.IntegerField(default=24)
    assignments_per_hit = models.IntegerField(default=1)
    date_published = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=1024)
    hit_template = models.ForeignKey('HitTemplate')
    name = models.CharField(max_length=1024)

    def available_hits_for(self, user):
        """Retrieve a list of all HITs in this batch available for the user.

        This list DOES NOT include HITs in the batch that have been assigned
        to the user but not yet completed.

        Args:
            user (User|AnonymousUser):

        Returns:
            QuerySet of Hit objects
        """
        if not user.is_authenticated and self.hit_template.login_required:
            return Hit.objects.none()

        hs = self.hit_set.filter(completed=False)

        # Exclude HITs that have already been assigned to this user.
        if user.is_authenticated:
            # If the user is not authenticated, then user.id is None,
            # and the query below would exclude all uncompleted HITs.
            hs = hs.exclude(hitassignment__assigned_to_id=user.id)

        # Only include HITs whose total (possibly not completed) assignments < assignments_per_hit
        hs = hs.annotate(ac=models.Count('hitassignment')).filter(ac__lt=self.assignments_per_hit)

        return hs

    def clean(self):
        if not self.hit_template.login_required and self.assignments_per_hit != 1:
            raise ValidationError('When login is not required to access the Template, ' +
                                  'the number of Assignments per HIT must be 1')

    def csv_results_filename(self):
        """Returns filename for CSV results file for this HitBatch
        """
        batch_filename, extension = os.path.splitext(os.path.basename(self.filename))

        # We are following Mechanical Turk's naming conventions for results files
        return "{}-Batch_{}_results{}".format(batch_filename, self.id, extension)

    def create_hits_from_csv(self, csv_fh):
        """
        Args:
            csv_fh (file-like object): File handle for CSV input

        Returns:
            Number of HITs created from CSV file
        """
        header, data_rows = self._parse_csv(csv_fh)

        num_created_hits = 0
        for row in data_rows:
            if not row:
                continue
            hit = Hit(
                hit_batch=self,
                input_csv_fields=dict(zip(header, row)),
            )
            hit.save()
            num_created_hits += 1

        return num_created_hits

    def expire_assignments(self):
        HitAssignment.objects.\
            filter(completed=False).\
            filter(hit__hit_batch_id=self.id).\
            filter(expires_at__lt=timezone.now()).\
            delete()

    def finished_hits(self):
        """
        Returns:
            QuerySet of all Hit objects associated with this HitBatch
            that have been completed.
        """
        return self.hit_set.filter(completed=True).order_by('-id')

    def next_available_hit_for(self, user):
        """Returns next available HIT for the user, or None if no HITs available

        Args:
            user (User):

        Returns:
            Hit|None
        """
        return self.available_hits_for(user).first()

    def total_available_hits_for(self, user):
        """Returns number of HITs available for the user

        Args:
            user (User):

        Returns:
            Number of HITs available for user
        """
        return self.available_hits_for(user).count()

    def total_finished_hits(self):
        return self.finished_hits().count()

    def total_hits(self):
        return self.hit_set.count()

    def to_csv(self, csv_fh):
        """Write CSV output to file handle for every Hit in batch

        Args:
            csv_fh (file-like object): File handle for CSV output
        """
        fieldnames, rows = self._results_data(self.finished_hits())
        writer = unicodecsv.DictWriter(csv_fh, fieldnames, quoting=unicodecsv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    def unfinished_hits(self):
        """
        Returns:
            QuerySet of all Hit objects associated with this HitBatch
            that have NOT been completed.
        """
        return self.hit_set.filter(completed=False).order_by('id')

    def _parse_csv(self, csv_fh):
        """
        Args:
            csv_fh (file-like object): File handle for CSV output

        Returns:
            A tuple where the first value is a list of strings for the
            header fieldnames, and the second value is an iterable
            that returns a list of values for the rest of the roww in
            the CSV file.
        """
        rows = unicodecsv.reader(csv_fh)
        header = next(rows)
        return header, rows

    def _get_csv_fieldnames(self, hits):
        """
        Args:
            hits (List of Hit objects):

        Returns:
            A tuple of strings specifying the fieldnames to be used in
            in the header of a CSV file.
        """
        input_field_set = set()
        answer_field_set = set()
        for hit in hits:
            for hit_assignment in hit.hitassignment_set.all():
                input_field_set.update(hit.input_csv_fields.keys())
                answer_field_set.update(hit_assignment.answers.keys())
        return tuple(
            [u'Input.' + k for k in sorted(input_field_set)] +
            [u'Answer.' + k for k in sorted(answer_field_set)]
        )

    def _results_data(self, hits):
        """
        All completed HITs must come from the same template so that they have the
        same field names.

        Args:
            hits (List of Hit objects):

        Returns:
            A tuple where the first value is a list of fieldname strings, and
            the second value is a list of dicts, where the keys to these
            dicts are the values of the fieldname strings.
        """
        rows = []
        for hit in hits:
            for hit_assignment in hit.hitassignment_set.all():
                row = {}
                row.update({u'Input.' + k: v for k, v in hit.input_csv_fields.items()})
                row.update({u'Answer.' + k: v for k, v in hit_assignment.answers.items()})
                rows.append(row)

        return self._get_csv_fieldnames(hits), rows

    def __unicode__(self):
        return 'HIT Batch: {}'.format(self.name)

    def __str__(self):
        return 'HIT Batch: {}'.format(self.name)


class HitTemplate(models.Model):
    class Meta:
        verbose_name = "HIT template"

    active = models.BooleanField(default=True)
    assignments_per_hit = models.IntegerField(default=1)
    date_modified = models.DateTimeField(auto_now=True)
    filename = models.CharField(max_length=1024, blank=True)
    form = models.TextField()
    form_has_submit_button = models.BooleanField(default=False)
    login_required = models.BooleanField(default=True)
    name = models.CharField(max_length=1024)

    # Fieldnames are automatically extracted from form text
    fieldnames = JSONField(blank=True)

    @classmethod
    def available_for(cls, user):
        """Retrieve the HitTemplates that the user has permission to access

        Args:
            user (User):

        Returns:
            QuerySet of HitTemplate objects this user can access
        """
        templates = cls.objects.filter(active=True)
        if not user.is_authenticated:
            templates = templates.filter(login_required=False)
        return templates

    def batches_available_for(self, user):
        """Retrieve the HitBatches that the user has permission to access

        Args:
            user (User):

        Returns:
            QuerySet of HitBatch objects this usre can access
        """
        batches = self.hitbatch_set.filter(active=True)
        if not user.is_authenticated:
            batches = batches.filter(hit_template__login_required=False)
        return batches

    def clean(self):
        if not self.login_required and self.assignments_per_hit != 1:
            raise ValidationError('When login is not required to access the Template, ' +
                                  'the number of Assignments per HIT must be 1')

    def save(self, *args, **kwargs):
        soup = BeautifulSoup(self.form, 'html.parser')
        self.form_has_submit_button = bool(soup.select('input[type=submit]'))

        # Extract fieldnames from form text, save fieldnames as keys of JSON dict
        unique_fieldnames = set(re.findall(r'\${(\w+)}', self.form))
        self.fieldnames = dict((fn, True) for fn in unique_fieldnames)
        super(HitTemplate, self).save(*args, **kwargs)

    def to_csv(self, csv_fh):
        """
        Writes CSV output to file handle for every Hit associated with template

        Args:
            csv_fh (file-like object): File handle for CSV output
        """
        batches = self.hitbatch_set.all()
        if batches:
            fieldnames = self._get_csv_fieldnames(batches)
            writer = unicodecsv.DictWriter(csv_fh, fieldnames, quoting=unicodecsv.QUOTE_ALL)
            writer.writeheader()
            for batch in batches:
                _, rows = batch._results_data(batch.finished_hits())
                for row in rows:
                    writer.writerow(row)

    def _get_csv_fieldnames(self, batches):
        """
        Args:
            batches (List of HitBatch objects)

        Returns:
            A tuple of strings specifying the fieldnames to be used in
            in the header of a CSV file.
        """
        input_field_set = set()
        answer_field_set = set()
        for batch in batches:
            for hit in batch.hit_set.all():
                for hit_assignment in hit.hitassignment_set.all():
                    input_field_set.update(hit.input_csv_fields.keys())
                    answer_field_set.update(hit_assignment.answers.keys())
        return tuple(
            [u'Input.' + k for k in sorted(input_field_set)] +
            [u'Answer.' + k for k in sorted(answer_field_set)]
        )

    def __unicode__(self):
        return 'HIT Template: {}'.format(self.name)

    def __str__(self):
        return 'HIT Template: {}'.format(self.name)
