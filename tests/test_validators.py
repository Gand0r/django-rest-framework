import datetime
import re
from unittest.mock import MagicMock, patch

import pytest
from django import VERSION as django_version
from django.db import DataError, models
from django.test import TestCase

from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.validators import (
    BaseUniqueForValidator, UniqueTogetherValidator, UniqueValidator, qs_exists
)


def dedent(blocktext):
    return '\n'.join([line[12:] for line in blocktext.splitlines()[1:-1]])


# Tests for `UniqueValidator`
# ---------------------------

class UniquenessModel(models.Model):
    username = models.CharField(unique=True, max_length=100)


class UniquenessSerializer(serializers.ModelSerializer):
    class Meta:
        model = UniquenessModel
        fields = '__all__'


class RelatedModel(models.Model):
    user = models.OneToOneField(UniquenessModel, on_delete=models.CASCADE)
    email = models.CharField(unique=True, max_length=80)


class RelatedModelSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username',
        validators=[UniqueValidator(queryset=UniquenessModel.objects.all(), lookup='iexact')])  # NOQA

    class Meta:
        model = RelatedModel
        fields = ('username', 'email')


class RelatedModelUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = RelatedModel
        fields = ('user',)


class AnotherUniquenessModel(models.Model):
    code = models.IntegerField(unique=True)


class AnotherUniquenessSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnotherUniquenessModel
        fields = '__all__'


class IntegerFieldModel(models.Model):
    integer = models.IntegerField()


class UniquenessIntegerSerializer(serializers.Serializer):
    # Note that this field *deliberately* does not correspond with the model field.
    # This allows us to ensure that `ValueError`, `TypeError` or `DataError` etc
    # raised by a uniqueness check does not trigger a deceptive "this field is not unique"
    # validation failure.
    integer = serializers.CharField(validators=[UniqueValidator(queryset=IntegerFieldModel.objects.all())])


class TestUniquenessValidation(TestCase):
    def setUp(self):
        self.instance = UniquenessModel.objects.create(username='existing')

    def test_repr(self):
        serializer = UniquenessSerializer()
        expected = dedent("""
            UniquenessSerializer():
                id = IntegerField(label='ID', read_only=True)
                username = CharField(max_length=100, validators=[<UniqueValidator(queryset=UniquenessModel.objects.all())>])
        """)
        assert repr(serializer) == expected

    def test_is_not_unique(self):
        data = {'username': 'existing'}
        serializer = UniquenessSerializer(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == {'username': ['uniqueness model with this username already exists.']}

    def test_relation_is_not_unique(self):
        RelatedModel.objects.create(user=self.instance)
        data = {'user': self.instance.pk}
        serializer = RelatedModelUserSerializer(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == {'user': ['related model with this user already exists.']}

    def test_is_unique(self):
        data = {'username': 'other'}
        serializer = UniquenessSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {'username': 'other'}

    def test_updated_instance_excluded(self):
        data = {'username': 'existing'}
        serializer = UniquenessSerializer(self.instance, data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {'username': 'existing'}

    def test_doesnt_pollute_model(self):
        instance = AnotherUniquenessModel.objects.create(code='100')
        serializer = AnotherUniquenessSerializer(instance)
        assert all(
            ["Unique" not in repr(v) for v in AnotherUniquenessModel._meta.get_field('code').validators]
        )

        # Accessing data shouldn't effect validators on the model
        serializer.data
        assert all(
            ["Unique" not in repr(v) for v in AnotherUniquenessModel._meta.get_field('code').validators]
        )

    def test_related_model_is_unique(self):
        data = {'username': 'Existing', 'email': 'new-email@example.com'}
        rs = RelatedModelSerializer(data=data)
        assert not rs.is_valid()
        assert rs.errors == {'username': ['This field must be unique.']}
        data = {'username': 'new-username', 'email': 'new-email@example.com'}
        rs = RelatedModelSerializer(data=data)
        assert rs.is_valid()

    def test_value_error_treated_as_not_unique(self):
        serializer = UniquenessIntegerSerializer(data={'integer': 'abc'})
        assert serializer.is_valid()


# Tests for `UniqueTogetherValidator`
# -----------------------------------

class UniquenessTogetherModel(models.Model):
    race_name = models.CharField(max_length=100)
    position = models.IntegerField()

    class Meta:
        unique_together = ('race_name', 'position')


class NullUniquenessTogetherModel(models.Model):
    """
    Used to ensure that null values are not included when checking
    unique_together constraints.

    Ignoring items which have a null in any of the validated fields is the same
    behavior that database backends will use when they have the
    unique_together constraint added.

    Example case: a null position could indicate a non-finisher in the race,
    there could be many non-finishers in a race, but all non-NULL
    values *should* be unique against the given `race_name`.
    """
    date_of_birth = models.DateField(null=True)  # Not part of the uniqueness constraint
    race_name = models.CharField(max_length=100)
    position = models.IntegerField(null=True)

    class Meta:
        unique_together = ('race_name', 'position')


class UniquenessTogetherSerializer(serializers.ModelSerializer):
    class Meta:
        model = UniquenessTogetherModel
        fields = '__all__'


class NullUniquenessTogetherSerializer(serializers.ModelSerializer):
    class Meta:
        model = NullUniquenessTogetherModel
        fields = '__all__'


class TestUniquenessTogetherValidation(TestCase):
    def setUp(self):
        self.instance = UniquenessTogetherModel.objects.create(
            race_name='example',
            position=1
        )
        UniquenessTogetherModel.objects.create(
            race_name='example',
            position=2
        )
        UniquenessTogetherModel.objects.create(
            race_name='other',
            position=1
        )

    def test_repr(self):
        serializer = UniquenessTogetherSerializer()
        expected = dedent(r"""
            UniquenessTogetherSerializer\(\):
                id = IntegerField\(label='ID', read_only=True\)
                race_name = CharField\(max_length=100, required=True\)
                position = IntegerField\(.*required=True\)
                class Meta:
                    validators = \[<UniqueTogetherValidator\(queryset=UniquenessTogetherModel.objects.all\(\), fields=\('race_name', 'position'\)\)>\]
        """)
        assert re.search(expected, repr(serializer)) is not None

    def test_is_not_unique_together(self):
        """
        Failing unique together validation should result in non field errors.
        """
        data = {'race_name': 'example', 'position': 2}
        serializer = UniquenessTogetherSerializer(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == {
            'non_field_errors': [
                'The fields race_name, position must make a unique set.'
            ]
        }

    def test_is_unique_together(self):
        """
        In a unique together validation, one field may be non-unique
        so long as the set as a whole is unique.
        """
        data = {'race_name': 'other', 'position': 2}
        serializer = UniquenessTogetherSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {
            'race_name': 'other',
            'position': 2
        }

    def test_updated_instance_excluded_from_unique_together(self):
        """
        When performing an update, the existing instance does not count
        as a match against uniqueness.
        """
        data = {'race_name': 'example', 'position': 1}
        serializer = UniquenessTogetherSerializer(self.instance, data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {
            'race_name': 'example',
            'position': 1
        }

    def test_unique_together_is_required(self):
        """
        In a unique together validation, all fields are required.
        """
        data = {'position': 2}
        serializer = UniquenessTogetherSerializer(data=data, partial=True)
        assert not serializer.is_valid()
        assert serializer.errors == {
            'race_name': ['This field is required.']
        }

    def test_ignore_excluded_fields(self):
        """
        When model fields are not included in a serializer, then uniqueness
        validators should not be added for that field.
        """
        class ExcludedFieldSerializer(serializers.ModelSerializer):
            class Meta:
                model = UniquenessTogetherModel
                fields = ('id', 'race_name',)
        serializer = ExcludedFieldSerializer()
        expected = dedent("""
            ExcludedFieldSerializer():
                id = IntegerField(label='ID', read_only=True)
                race_name = CharField(max_length=100)
        """)
        assert repr(serializer) == expected

    def test_ignore_read_only_fields(self):
        """
        When serializer fields are read only, then uniqueness
        validators should not be added for that field.
        """
        class ReadOnlyFieldSerializer(serializers.ModelSerializer):
            class Meta:
                model = UniquenessTogetherModel
                fields = ('id', 'race_name', 'position')
                read_only_fields = ('race_name',)

        serializer = ReadOnlyFieldSerializer()
        expected = dedent(r"""
            ReadOnlyFieldSerializer\(\):
                id = IntegerField\(label='ID', read_only=True\)
                race_name = CharField\(read_only=True\)
                position = IntegerField\(.*required=True\)
        """)
        assert re.search(expected, repr(serializer)) is not None

    def test_read_only_fields_with_default(self):
        """
        Special case of read_only + default DOES validate unique_together.
        """
        class ReadOnlyFieldWithDefaultSerializer(serializers.ModelSerializer):
            race_name = serializers.CharField(max_length=100, read_only=True, default='example')

            class Meta:
                model = UniquenessTogetherModel
                fields = ('id', 'race_name', 'position')

        data = {'position': 2}
        serializer = ReadOnlyFieldWithDefaultSerializer(data=data)

        assert len(serializer.validators) == 1
        assert isinstance(serializer.validators[0], UniqueTogetherValidator)
        assert serializer.validators[0].fields == ('race_name', 'position')
        assert not serializer.is_valid()
        assert serializer.errors == {
            'non_field_errors': [
                'The fields race_name, position must make a unique set.'
            ]
        }

    def test_read_only_fields_with_default_and_source(self):
        class ReadOnlySerializer(serializers.ModelSerializer):
            name = serializers.CharField(source='race_name', default='test', read_only=True)

            class Meta:
                model = UniquenessTogetherModel
                fields = ['name', 'position']
                validators = [
                    UniqueTogetherValidator(
                        queryset=UniquenessTogetherModel.objects.all(),
                        fields=['name', 'position']
                    )
                ]

        serializer = ReadOnlySerializer(data={'position': 1})
        assert serializer.is_valid(raise_exception=True)

    def test_writeable_fields_with_source(self):
        class WriteableSerializer(serializers.ModelSerializer):
            name = serializers.CharField(source='race_name')

            class Meta:
                model = UniquenessTogetherModel
                fields = ['name', 'position']
                validators = [
                    UniqueTogetherValidator(
                        queryset=UniquenessTogetherModel.objects.all(),
                        fields=['name', 'position']
                    )
                ]

        serializer = WriteableSerializer(data={'name': 'test', 'position': 1})
        assert serializer.is_valid(raise_exception=True)

        # Validation error should use seriazlier field name, not source
        serializer = WriteableSerializer(data={'position': 1})
        assert not serializer.is_valid()
        assert serializer.errors == {
            'name': [
                'This field is required.'
            ]
        }

    def test_default_validator_with_fields_with_source(self):
        class TestSerializer(serializers.ModelSerializer):
            name = serializers.CharField(source='race_name')

            class Meta:
                model = UniquenessTogetherModel
                fields = ['name', 'position']

        serializer = TestSerializer()
        expected = dedent(r"""
            TestSerializer\(\):
                name = CharField\(source='race_name'\)
                position = IntegerField\(.*\)
                class Meta:
                    validators = \[<UniqueTogetherValidator\(queryset=UniquenessTogetherModel.objects.all\(\), fields=\('name', 'position'\)\)>\]
        """)
        assert re.search(expected, repr(serializer)) is not None

    def test_default_validator_with_multiple_fields_with_same_source(self):
        class TestSerializer(serializers.ModelSerializer):
            name = serializers.CharField(source='race_name')
            other_name = serializers.CharField(source='race_name')

            class Meta:
                model = UniquenessTogetherModel
                fields = ['name', 'other_name', 'position']

        serializer = TestSerializer(data={
            'name': 'foo',
            'other_name': 'foo',
            'position': 1,
        })
        with pytest.raises(AssertionError) as excinfo:
            serializer.is_valid()

        expected = (
            "Unable to create `UniqueTogetherValidator` for "
            "`UniquenessTogetherModel.race_name` as `TestSerializer` has "
            "multiple fields (name, other_name) that map to this model field. "
            "Either remove the extra fields, or override `Meta.validators` "
            "with a `UniqueTogetherValidator` using the desired field names.")
        assert str(excinfo.value) == expected

    def test_allow_explicit_override(self):
        """
        Ensure validators can be explicitly removed..
        """
        class NoValidatorsSerializer(serializers.ModelSerializer):
            class Meta:
                model = UniquenessTogetherModel
                fields = ('id', 'race_name', 'position')
                validators = []

        serializer = NoValidatorsSerializer()
        expected = dedent(r"""
            NoValidatorsSerializer\(\):
                id = IntegerField\(label='ID', read_only=True.*\)
                race_name = CharField\(max_length=100\)
                position = IntegerField\(.*\)
        """)
        assert re.search(expected, repr(serializer)) is not None

    def test_ignore_validation_for_null_fields(self):
        # None values that are on fields which are part of the uniqueness
        # constraint cause the instance to ignore uniqueness validation.
        NullUniquenessTogetherModel.objects.create(
            date_of_birth=datetime.date(2000, 1, 1),
            race_name='Paris Marathon',
            position=None
        )
        data = {
            'date': datetime.date(2000, 1, 1),
            'race_name': 'Paris Marathon',
            'position': None
        }
        serializer = NullUniquenessTogetherSerializer(data=data)
        assert serializer.is_valid()

    def test_ignore_validation_for_missing_nullable_fields(self):
        data = {
            'date': datetime.date(2000, 1, 1),
            'race_name': 'Paris Marathon',
        }
        serializer = NullUniquenessTogetherSerializer(data=data)
        assert serializer.is_valid(), serializer.errors

    def test_do_not_ignore_validation_for_null_fields(self):
        # None values that are not on fields part of the uniqueness constraint
        # do not cause the instance to skip validation.
        NullUniquenessTogetherModel.objects.create(
            date_of_birth=datetime.date(2000, 1, 1),
            race_name='Paris Marathon',
            position=1
        )
        data = {'date': None, 'race_name': 'Paris Marathon', 'position': 1}
        serializer = NullUniquenessTogetherSerializer(data=data)
        assert not serializer.is_valid()

    def test_ignore_validation_for_unchanged_fields(self):
        """
        If all fields in the unique together constraint are unchanged,
        then the instance should skip uniqueness validation.
        """
        instance = UniquenessTogetherModel.objects.create(
            race_name="Paris Marathon", position=1
        )
        data = {"race_name": "Paris Marathon", "position": 1}
        serializer = UniquenessTogetherSerializer(data=data, instance=instance)
        with patch(
            "rest_framework.validators.qs_exists"
        ) as mock:
            assert serializer.is_valid()
            assert not mock.called

    @patch("rest_framework.validators.qs_exists")
    def test_unique_together_with_source(self, mock_qs_exists):
        class UniqueTogetherWithSourceSerializer(serializers.ModelSerializer):
            name = serializers.CharField(source="race_name")
            pos = serializers.IntegerField(source="position")

            class Meta:
                model = UniquenessTogetherModel
                fields = ["name", "pos"]

        data = {"name": "Paris Marathon", "pos": 1}
        instance = UniquenessTogetherModel.objects.create(
            race_name="Paris Marathon", position=1
        )
        serializer = UniqueTogetherWithSourceSerializer(data=data)
        assert not serializer.is_valid()
        assert mock_qs_exists.called
        mock_qs_exists.reset_mock()
        serializer = UniqueTogetherWithSourceSerializer(data=data, instance=instance)
        assert serializer.is_valid()
        assert not mock_qs_exists.called

    def test_filter_queryset_do_not_skip_existing_attribute(self):
        """
        filter_queryset should add value from existing instance attribute
        if it is not provided in attributes dict
        """
        class MockQueryset:
            def filter(self, **kwargs):
                self.called_with = kwargs

        data = {'race_name': 'bar'}
        queryset = MockQueryset()
        serializer = UniquenessTogetherSerializer(instance=self.instance)
        validator = UniqueTogetherValidator(queryset, fields=('race_name',
                                                              'position'))
        validator.filter_queryset(attrs=data, queryset=queryset, serializer=serializer)
        assert queryset.called_with == {'race_name': 'bar', 'position': 1}

    def test_uniq_together_validation_uses_model_fields_method_field(self):
        class TestSerializer(serializers.ModelSerializer):
            position = serializers.SerializerMethodField()

            def get_position(self, obj):
                return obj.position or 0

            class Meta:
                model = NullUniquenessTogetherModel
                fields = ['race_name', 'position']

        serializer = TestSerializer()
        expected = dedent("""
            TestSerializer():
                race_name = CharField(max_length=100)
                position = SerializerMethodField()
        """)
        assert repr(serializer) == expected

    def test_uniq_together_validation_uses_model_fields_with_source_field(self):
        class TestSerializer(serializers.ModelSerializer):
            pos = serializers.IntegerField(source='position')

            class Meta:
                model = NullUniquenessTogetherModel
                fields = ['race_name', 'pos']

        serializer = TestSerializer()
        expected = dedent("""
            TestSerializer():
                race_name = CharField(max_length=100, required=True)
                pos = IntegerField(source='position')
                class Meta:
                    validators = [<UniqueTogetherValidator(queryset=NullUniquenessTogetherModel.objects.all(), fields=('race_name', 'pos'))>]
        """)
        assert repr(serializer) == expected


class UniqueConstraintModel(models.Model):
    race_name = models.CharField(max_length=100)
    position = models.IntegerField()
    global_id = models.IntegerField()
    fancy_conditions = models.IntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                name="unique_constraint_model_global_id_uniq",
                fields=('global_id',),
            ),
            models.UniqueConstraint(
                name="unique_constraint_model_fancy_1_uniq",
                fields=('fancy_conditions',),
                condition=models.Q(global_id__lte=1)
            ),
            models.UniqueConstraint(
                name="unique_constraint_model_fancy_3_uniq",
                fields=('fancy_conditions',),
                condition=models.Q(global_id__gte=3)
            ),
            models.UniqueConstraint(
                name="unique_constraint_model_together_uniq",
                fields=('race_name', 'position'),
                condition=models.Q(race_name='example'),
            ),
            models.UniqueConstraint(
                name='unique_constraint_model_together_uniq2',
                fields=('race_name', 'position'),
                condition=models.Q(fancy_conditions__gte=10),
            ),
        ]


class UniqueConstraintNullableModel(models.Model):
    title = models.CharField(max_length=100)
    age = models.IntegerField(null=True)
    tag = models.CharField(max_length=100, null=True)

    class Meta:
        constraints = [
            # Unique constraint on 2 nullable fields
            models.UniqueConstraint(name='unique_constraint', fields=('age', 'tag'))
        ]


class UniqueConstraintSerializer(serializers.ModelSerializer):
    class Meta:
        model = UniqueConstraintModel
        fields = '__all__'


class UniqueConstraintNullableSerializer(serializers.ModelSerializer):
    class Meta:
        model = UniqueConstraintNullableModel
        fields = ('title', 'age', 'tag')


class TestUniqueConstraintValidation(TestCase):
    def setUp(self):
        self.instance = UniqueConstraintModel.objects.create(
            race_name='example',
            position=1,
            global_id=1,
            fancy_conditions=1
        )
        UniqueConstraintModel.objects.create(
            race_name='example',
            position=2,
            global_id=2,
            fancy_conditions=1
        )
        UniqueConstraintModel.objects.create(
            race_name='other',
            position=1,
            global_id=3,
            fancy_conditions=1
        )

    def test_repr(self):
        serializer = UniqueConstraintSerializer()
        # the order of validators isn't deterministic so delete
        # fancy_conditions field that has two of them
        del serializer.fields['fancy_conditions']
        expected = dedent(r"""
            UniqueConstraintSerializer\(\):
                id = IntegerField\(label='ID', read_only=True\)
                race_name = CharField\(max_length=100, required=True\)
                position = IntegerField\(.*required=True\)
                global_id = IntegerField\(.*validators=\[<UniqueValidator\(queryset=UniqueConstraintModel.objects.all\(\)\)>\]\)
                class Meta:
                    validators = \[<UniqueTogetherValidator\(queryset=UniqueConstraintModel.objects.all\(\), fields=\('race_name', 'position'\), condition=<Q: \(AND: \('race_name', 'example'\)\)>\)>\]
        """)
        assert re.search(expected, repr(serializer)) is not None

    def test_unique_together_condition(self):
        """
        Fields used in UniqueConstraint's condition must be included
        into queryset existence check
        """
        UniqueConstraintModel.objects.create(
            race_name='condition',
            position=1,
            global_id=10,
            fancy_conditions=10,
        )
        serializer = UniqueConstraintSerializer(data={
            'race_name': 'condition',
            'position': 1,
            'global_id': 11,
            'fancy_conditions': 9,
        })
        assert serializer.is_valid()
        serializer = UniqueConstraintSerializer(data={
            'race_name': 'condition',
            'position': 1,
            'global_id': 11,
            'fancy_conditions': 11,
        })
        assert not serializer.is_valid()

    def test_unique_together_condition_fields_required(self):
        """
        Fields used in UniqueConstraint's condition must be present in serializer
        """
        serializer = UniqueConstraintSerializer(data={
            'race_name': 'condition',
            'position': 1,
            'global_id': 11,
        })
        assert not serializer.is_valid()
        assert serializer.errors == {'fancy_conditions': ['This field is required.']}

        class NoFieldsSerializer(serializers.ModelSerializer):
            class Meta:
                model = UniqueConstraintModel
                fields = ('race_name', 'position', 'global_id')

        serializer = NoFieldsSerializer()
        assert len(serializer.validators) == 1

    def test_single_field_uniq_validators(self):
        """
        UniqueConstraint with single field must be transformed into
        field's UniqueValidator
        """
        # Django 5 includes Max and Min values validators for IntegerField
        extra_validators_qty = 2 if django_version[0] >= 5 else 0
        serializer = UniqueConstraintSerializer()
        assert len(serializer.validators) == 2
        validators = serializer.fields['global_id'].validators
        assert len(validators) == 1 + extra_validators_qty
        assert validators[0].queryset == UniqueConstraintModel.objects

        validators = serializer.fields['fancy_conditions'].validators
        assert len(validators) == 2 + extra_validators_qty
        ids_in_qs = {frozenset(v.queryset.values_list(flat=True)) for v in validators if hasattr(v, "queryset")}
        assert ids_in_qs == {frozenset([1]), frozenset([3])}

    def test_nullable_unique_constraint_fields_are_not_required(self):
        serializer = UniqueConstraintNullableSerializer(data={'title': 'Bob'})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        result = serializer.save()
        self.assertIsInstance(result, UniqueConstraintNullableModel)

    def test_unique_constraint_source(self):
        class SourceUniqueConstraintSerializer(serializers.ModelSerializer):
            raceName = serializers.CharField(source="race_name")

            class Meta:
                model = UniqueConstraintModel
                fields = ("raceName", "position", "global_id", "fancy_conditions")

        serializer = SourceUniqueConstraintSerializer(
            data={
                "raceName": "example",
                "position": 5,
                "global_id": 11,
                "fancy_conditions": 11,
            }
        )
        assert serializer.is_valid()


# Tests for `UniqueForDateValidator`
# ----------------------------------

class UniqueForDateModel(models.Model):
    slug = models.CharField(max_length=100, unique_for_date='published')
    published = models.DateField()


class UniqueForDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = UniqueForDateModel
        fields = '__all__'


class TestUniquenessForDateValidation(TestCase):
    def setUp(self):
        self.instance = UniqueForDateModel.objects.create(
            slug='existing',
            published='2000-01-01'
        )

    def test_repr(self):
        serializer = UniqueForDateSerializer()
        expected = dedent("""
            UniqueForDateSerializer():
                id = IntegerField(label='ID', read_only=True)
                slug = CharField(max_length=100)
                published = DateField(required=True)
                class Meta:
                    validators = [<UniqueForDateValidator(queryset=UniqueForDateModel.objects.all(), field='slug', date_field='published')>]
        """)
        assert repr(serializer) == expected

    def test_is_not_unique_for_date(self):
        """
        Failing unique for date validation should result in field error.
        """
        data = {'slug': 'existing', 'published': '2000-01-01'}
        serializer = UniqueForDateSerializer(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == {
            'slug': ['This field must be unique for the "published" date.']
        }

    def test_is_unique_for_date(self):
        """
        Passing unique for date validation.
        """
        data = {'slug': 'existing', 'published': '2000-01-02'}
        serializer = UniqueForDateSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {
            'slug': 'existing',
            'published': datetime.date(2000, 1, 2)
        }

    def test_updated_instance_excluded_from_unique_for_date(self):
        """
        When performing an update, the existing instance does not count
        as a match against unique_for_date.
        """
        data = {'slug': 'existing', 'published': '2000-01-01'}
        serializer = UniqueForDateSerializer(instance=self.instance, data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {
            'slug': 'existing',
            'published': datetime.date(2000, 1, 1)
        }

# Tests for `UniqueForMonthValidator`
# ----------------------------------


class UniqueForMonthModel(models.Model):
    slug = models.CharField(max_length=100, unique_for_month='published')
    published = models.DateField()


class UniqueForMonthSerializer(serializers.ModelSerializer):
    class Meta:
        model = UniqueForMonthModel
        fields = '__all__'


class UniqueForMonthTests(TestCase):

    def setUp(self):
        self.instance = UniqueForMonthModel.objects.create(
            slug='existing', published='2017-01-01'
        )

    def test_not_unique_for_month(self):
        data = {'slug': 'existing', 'published': '2017-01-01'}
        serializer = UniqueForMonthSerializer(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == {
            'slug': ['This field must be unique for the "published" month.']
        }

    def test_unique_for_month(self):
        data = {'slug': 'existing', 'published': '2017-02-01'}
        serializer = UniqueForMonthSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {
            'slug': 'existing',
            'published': datetime.date(2017, 2, 1)
        }

# Tests for `UniqueForYearValidator`
# ----------------------------------


class UniqueForYearModel(models.Model):
    slug = models.CharField(max_length=100, unique_for_year='published')
    published = models.DateField()


class UniqueForYearSerializer(serializers.ModelSerializer):
    class Meta:
        model = UniqueForYearModel
        fields = '__all__'


class UniqueForYearTests(TestCase):

    def setUp(self):
        self.instance = UniqueForYearModel.objects.create(
            slug='existing', published='2017-01-01'
        )

    def test_not_unique_for_year(self):
        data = {'slug': 'existing', 'published': '2017-01-01'}
        serializer = UniqueForYearSerializer(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == {
            'slug': ['This field must be unique for the "published" year.']
        }

    def test_unique_for_year(self):
        data = {'slug': 'existing', 'published': '2018-01-01'}
        serializer = UniqueForYearSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data == {
            'slug': 'existing',
            'published': datetime.date(2018, 1, 1)
        }


class HiddenFieldUniqueForDateModel(models.Model):
    slug = models.CharField(max_length=100, unique_for_date='published')
    published = models.DateTimeField(auto_now_add=True)


class TestHiddenFieldUniquenessForDateValidation(TestCase):
    def test_repr_date_field_not_included(self):
        class TestSerializer(serializers.ModelSerializer):
            class Meta:
                model = HiddenFieldUniqueForDateModel
                fields = ('id', 'slug')

        serializer = TestSerializer()
        expected = dedent("""
            TestSerializer():
                id = IntegerField(label='ID', read_only=True)
                slug = CharField(max_length=100)
                published = HiddenField(default=CreateOnlyDefault(<function now>))
                class Meta:
                    validators = [<UniqueForDateValidator(queryset=HiddenFieldUniqueForDateModel.objects.all(), field='slug', date_field='published')>]
        """)
        assert repr(serializer) == expected

    def test_repr_date_field_included(self):
        class TestSerializer(serializers.ModelSerializer):
            class Meta:
                model = HiddenFieldUniqueForDateModel
                fields = ('id', 'slug', 'published')

        serializer = TestSerializer()
        expected = dedent("""
            TestSerializer():
                id = IntegerField(label='ID', read_only=True)
                slug = CharField(max_length=100)
                published = DateTimeField(default=CreateOnlyDefault(<function now>), read_only=True)
                class Meta:
                    validators = [<UniqueForDateValidator(queryset=HiddenFieldUniqueForDateModel.objects.all(), field='slug', date_field='published')>]
        """)
        assert repr(serializer) == expected


class ValidatorsTests(TestCase):

    def test_qs_exists_handles_type_error(self):
        class TypeErrorQueryset:
            def exists(self):
                raise TypeError
        assert qs_exists(TypeErrorQueryset()) is False

    def test_qs_exists_handles_value_error(self):
        class ValueErrorQueryset:
            def exists(self):
                raise ValueError
        assert qs_exists(ValueErrorQueryset()) is False

    def test_qs_exists_handles_data_error(self):
        class DataErrorQueryset:
            def exists(self):
                raise DataError
        assert qs_exists(DataErrorQueryset()) is False

    def test_validator_raises_error_if_not_all_fields_are_provided(self):
        validator = BaseUniqueForValidator(queryset=object(), field='foo',
                                           date_field='bar')
        attrs = {'foo': 'baz'}
        with pytest.raises(ValidationError):
            validator.enforce_required_fields(attrs)

    def test_validator_raises_error_when_abstract_method_called(self):
        validator = BaseUniqueForValidator(queryset=object(), field='foo',
                                           date_field='bar')
        with pytest.raises(NotImplementedError):
            validator.filter_queryset(
                attrs=None, queryset=None, field_name='', date_field_name=''
            )

    def test_equality_operator(self):
        mock_queryset = MagicMock()
        validator = BaseUniqueForValidator(queryset=mock_queryset, field='foo',
                                           date_field='bar')
        validator2 = BaseUniqueForValidator(queryset=mock_queryset, field='foo',
                                            date_field='bar')
        assert validator == validator2
        validator2.date_field = "bar2"
        assert validator != validator2
