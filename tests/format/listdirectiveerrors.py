import os
import pytest
from buildstream import _yaml
from buildstream import LoadError, LoadErrorReason
from tests.testutils.runcli import cli

# Project directory
DATA_DIR = os.path.dirname(os.path.realpath(__file__))


@pytest.mark.datafiles(DATA_DIR)
def test_project_error(cli, datafiles):
    project = os.path.join(datafiles.dirname, datafiles.basename, 'list-directive-error-project')
    result = cli.run(project=project, silent=True, args=[
        'show',
        '--deps', 'none',
        '--format', '%{vars}',
        'element.bst'])

    assert result.exit_code != 0
    assert result.exception
    assert isinstance(result.exception, LoadError)
    assert result.exception.reason == LoadErrorReason.TRAILING_LIST_DIRECTIVE


@pytest.mark.datafiles(DATA_DIR)
@pytest.mark.parametrize("target", [
    ('variables.bst'), ('environment.bst'), ('config.bst'), ('public.bst')
])
def test_element_error(cli, datafiles, target):
    project = os.path.join(datafiles.dirname, datafiles.basename, 'list-directive-error-element')
    result = cli.run(project=project, silent=True, args=[
        'show',
        '--deps', 'none',
        '--format', '%{vars}',
        target])

    assert result.exit_code != 0
    assert result.exception
    assert isinstance(result.exception, LoadError)
    assert result.exception.reason == LoadErrorReason.TRAILING_LIST_DIRECTIVE