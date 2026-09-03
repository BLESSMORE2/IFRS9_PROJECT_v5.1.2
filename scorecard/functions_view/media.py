from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, FileResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Q
from django.urls import reverse

from scorecard.functions_view.audit import log_evaluation_document_audit, log_scorecard_document_audit
from scorecard.functions_view.main_customer_lookup import get_request_branch_names
from scorecard.models import (
    AttributeResponseDocument,
    IFRS9AttributeResponseDocument,
    ScorecardDocument,
)


DOCUMENT_LIST_PAGE_SIZE_OPTIONS = (25, 50, 100)
DOCUMENT_FILTER_CACHE_TTL_SECONDS = 300


def _can_manage_documents(request: HttpRequest) -> bool:
    user = getattr(request, 'user', None)
    return bool(
        getattr(user, 'is_authenticated', False)
        and (getattr(user, 'is_superuser', False) or user.has_perm('scorecard.manage_scorecard_documents'))
    )


def _normalize_page_size(raw_value, default: int = 25) -> int:
    try:
        page_size = int(raw_value or default)
    except (TypeError, ValueError):
        page_size = default
    return page_size if page_size in DOCUMENT_LIST_PAGE_SIZE_OPTIONS else default


def _query_string_without_page(request: HttpRequest) -> str:
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _document_filter_cache_key(prefix: str, branch_names: list[str] | None = None) -> str:
    scope_token = "all"
    if branch_names is not None:
        scope_token = "|".join(
            sorted({(name or "").strip() for name in branch_names if (name or "").strip()})
        ) or "none"
    return f"scorecard:documents:{prefix}:{scope_token}"


def _get_document_customer_codes(branch_names: list[str] | None = None) -> list[str]:
    cache_key = _document_filter_cache_key("customer_codes", branch_names)
    cached_codes = cache.get(cache_key)
    if cached_codes is not None:
        return cached_codes

    basel_queryset = AttributeResponseDocument.objects.all()
    if branch_names is not None and not branch_names:
        basel_queryset = basel_queryset.none()
    elif branch_names:
        basel_queryset = basel_queryset.filter(
            attribute_response__evaluation__branch_name__in=branch_names
        )
    customer_codes_basel = set(
        basel_queryset.exclude(
            attribute_response__evaluation__customer_id__isnull=True
        ).exclude(
            attribute_response__evaluation__customer_id=''
        ).values_list(
            'attribute_response__evaluation__customer_id', flat=True
        ).distinct()
    )
    ifrs9_queryset = IFRS9AttributeResponseDocument.objects.all()
    if branch_names is not None and not branch_names:
        ifrs9_queryset = ifrs9_queryset.none()
    elif branch_names:
        ifrs9_queryset = ifrs9_queryset.filter(
            attribute_response__evaluation__branch_name__in=branch_names
        )
    customer_codes_ifrs9 = set(
        ifrs9_queryset.exclude(
            attribute_response__evaluation__customer_id__isnull=True
        ).exclude(
            attribute_response__evaluation__customer_id=''
        ).values_list(
            'attribute_response__evaluation__customer_id', flat=True
        ).distinct()
    )
    customer_codes = sorted(customer_codes_basel | customer_codes_ifrs9)
    cache.set(cache_key, customer_codes, DOCUMENT_FILTER_CACHE_TTL_SECONDS)
    return customer_codes


def _get_scorecard_document_categories():
    cache_key = _document_filter_cache_key("scorecard_categories")
    cached_categories = cache.get(cache_key)
    if cached_categories is not None:
        return cached_categories

    categories = list(
        ScorecardDocument.objects.filter(is_active=True).values_list(
            'category',
            flat=True
        ).distinct().exclude(
            category__isnull=True
        ).exclude(
            category=''
        ).order_by('category')
    )
    cache.set(cache_key, categories, DOCUMENT_FILTER_CACHE_TTL_SECONDS)
    return categories


def _serialize_evaluation_document(document, source_key: str) -> dict:
    attribute = document.attribute_response.attribute
    return {
        "id": document.id,
        "source_key": source_key,
        "source_label": "Basel Score Form" if source_key == "basel" else "IFRS9 Score Form",
        "source_badge_color": "#0066cc" if source_key == "basel" else "#28a745",
        "detail_url": reverse(
            "scorecard:document_detail" if source_key == "basel" else "scorecard:document_ifrs9_detail",
            kwargs={"document_id": document.id},
        ),
        "download_url": document.file.url,
        "delete_url": reverse(
            "scorecard:document_delete" if source_key == "basel" else "scorecard:document_ifrs9_delete",
            kwargs={"document_id": document.id},
        ),
        "file_name": document.file_name,
        "customer_id": document.attribute_response.evaluation.customer_id,
        "attribute_label": (
            attribute.group_label
            if attribute.group_label
            else f"{attribute.code} - {attribute.label}"
        ),
        "option_label": document.attribute_response.option.label if document.attribute_response.option else "-",
        "uploaded_by_email": document.uploaded_by.email if document.uploaded_by else "-",
        "uploaded_at": document.uploaded_at,
    }


def _next_document(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def _iter_merged_evaluation_documents(documents_basel, documents_ifrs9):
    basel_iterator = iter(documents_basel.iterator(chunk_size=200))
    ifrs9_iterator = iter(documents_ifrs9.iterator(chunk_size=200))
    basel_current = _next_document(basel_iterator)
    ifrs9_current = _next_document(ifrs9_iterator)

    while basel_current is not None or ifrs9_current is not None:
        if ifrs9_current is None or (
            basel_current is not None and basel_current.uploaded_at >= ifrs9_current.uploaded_at
        ):
            yield _serialize_evaluation_document(basel_current, "basel")
            basel_current = _next_document(basel_iterator)
        else:
            yield _serialize_evaluation_document(ifrs9_current, "ifrs9")
            ifrs9_current = _next_document(ifrs9_iterator)


@login_required
def document_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all Basel and IFRS9 evaluation documents with filtering by customer code,
    source, and server-side search.
    """
    # Get filter parameters
    search_query = request.GET.get('search', '').strip()
    customer_code = request.GET.get('customer_code', '').strip()
    source_filter = request.GET.get('source', 'all').strip()  # all, questionnaire, ifrs9
    page_size = _normalize_page_size(request.GET.get("page_size"))
    branch_names = get_request_branch_names(request)
    
    # Basel evaluation documents
    documents_basel = AttributeResponseDocument.objects.select_related(
        'attribute_response__evaluation',
        'attribute_response__attribute',
        'attribute_response__option',
        'uploaded_by'
    ).only(
        'id',
        'file',
        'file_name',
        'uploaded_at',
        'uploaded_by__email',
        'attribute_response__evaluation__customer_id',
        'attribute_response__attribute__group_label',
        'attribute_response__attribute__code',
        'attribute_response__attribute__label',
        'attribute_response__option__label',
    ).order_by('-uploaded_at')
    if not request.user.is_superuser and not branch_names:
        documents_basel = documents_basel.none()
    elif branch_names:
        documents_basel = documents_basel.filter(
            attribute_response__evaluation__branch_name__in=branch_names
        )

    if search_query:
        documents_basel = documents_basel.filter(
            Q(file_name__icontains=search_query)
            | Q(attribute_response__evaluation__customer_id__icontains=search_query)
            | Q(attribute_response__evaluation__customer_name__icontains=search_query)
            | Q(attribute_response__evaluation__branch_name__icontains=search_query)
            | Q(attribute_response__attribute__group_label__icontains=search_query)
            | Q(attribute_response__attribute__code__icontains=search_query)
            | Q(attribute_response__attribute__label__icontains=search_query)
            | Q(attribute_response__option__label__icontains=search_query)
            | Q(uploaded_by__email__icontains=search_query)
        )

    if customer_code:
        documents_basel = documents_basel.filter(
            attribute_response__evaluation__customer_id__icontains=customer_code
        )
    # IFRS9 evaluation documents
    documents_ifrs9 = IFRS9AttributeResponseDocument.objects.select_related(
        'attribute_response__evaluation',
        'attribute_response__attribute',
        'attribute_response__option',
        'uploaded_by'
    ).only(
        'id',
        'file',
        'file_name',
        'uploaded_at',
        'uploaded_by__email',
        'attribute_response__evaluation__customer_id',
        'attribute_response__attribute__group_label',
        'attribute_response__attribute__code',
        'attribute_response__attribute__label',
        'attribute_response__option__label',
    ).order_by('-uploaded_at')
    if not request.user.is_superuser and not branch_names:
        documents_ifrs9 = documents_ifrs9.none()
    elif branch_names:
        documents_ifrs9 = documents_ifrs9.filter(
            attribute_response__evaluation__branch_name__in=branch_names
        )

    if search_query:
        documents_ifrs9 = documents_ifrs9.filter(
            Q(file_name__icontains=search_query)
            | Q(attribute_response__evaluation__customer_id__icontains=search_query)
            | Q(attribute_response__evaluation__customer_name__icontains=search_query)
            | Q(attribute_response__evaluation__branch_name__icontains=search_query)
            | Q(attribute_response__attribute__group_label__icontains=search_query)
            | Q(attribute_response__attribute__code__icontains=search_query)
            | Q(attribute_response__attribute__label__icontains=search_query)
            | Q(attribute_response__option__label__icontains=search_query)
            | Q(uploaded_by__email__icontains=search_query)
        )

    if customer_code:
        documents_ifrs9 = documents_ifrs9.filter(
            attribute_response__evaluation__customer_id__icontains=customer_code
        )
    # Apply source filter
    if source_filter == 'questionnaire':
        documents_ifrs9 = documents_ifrs9.none()
    elif source_filter == 'ifrs9':
        documents_basel = documents_basel.none()
    
    # Get unique customer codes from both sources
    customer_codes = _get_document_customer_codes(branch_names)
    
    total_count = documents_basel.count() + documents_ifrs9.count()
    paginator = Paginator(range(total_count), page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_start = (page_obj.number - 1) * paginator.per_page
    page_end = page_start + paginator.per_page

    combined_documents = []
    for index, item in enumerate(_iter_merged_evaluation_documents(documents_basel, documents_ifrs9)):
        if index < page_start:
            continue
        if index >= page_end:
            break
        combined_documents.append(item)

    context = {
        'documents': combined_documents,
        'page_obj': page_obj,
        'page_start': page_start,
        'page_end': min(page_end, paginator.count),
        'query_string': _query_string_without_page(request),
        'customer_codes': customer_codes,
        'page_size': page_size,
        'filters': {
            'search': search_query,
            'customer_code': customer_code,
            'source': source_filter,
        },
        'total_count': total_count,
        'can_manage_documents': _can_manage_documents(request),
    }
    
    return render(
        request,
        'credit_scoreshifts/media/document_list.html',
        context,
    )


@login_required
def document_detail_view(request: HttpRequest, document_id: int) -> HttpResponse:
    """
    View details of a specific document.
    """
    document = get_object_or_404(
        AttributeResponseDocument.objects.select_related(
            'attribute_response__evaluation',
            'attribute_response__attribute',
            'attribute_response__option',
            'uploaded_by'
        ),
        id=document_id
    )
    
    context = {
        'document': document,
    }
    
    return render(
        request,
        'credit_scoreshifts/media/document_detail.html',
        context,
    )


@login_required
def document_ifrs9_detail_view(request: HttpRequest, document_id: int) -> HttpResponse:
    """
    View details of a specific IFRS9 score form document.
    """
    document = get_object_or_404(
        IFRS9AttributeResponseDocument.objects.select_related(
            'attribute_response__evaluation',
            'attribute_response__attribute',
            'attribute_response__option',
            'uploaded_by'
        ),
        id=document_id
    )
    
    context = {
        'document': document,
    }
    
    return render(
        request,
        'credit_scoreshifts/media/document_ifrs9_detail.html',
        context,
    )


# Scorecard Documents Views
@login_required
def scorecard_document_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all scorecard documents with filtering by category and server-side search.
    """
    # Get filter parameters
    category = request.GET.get('category', '').strip()
    search_query = request.GET.get('search', '').strip()
    page_size = _normalize_page_size(request.GET.get("page_size"))
    
    # Start with all active documents
    documents = ScorecardDocument.objects.filter(is_active=True).select_related(
        'uploaded_by'
    ).only(
        'id',
        'title',
        'description',
        'category',
        'file_name',
        'uploaded_at',
        'uploaded_by__email',
    ).order_by('-uploaded_at')
    
    # Apply filters
    if search_query:
        documents = documents.filter(
            Q(title__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(file_name__icontains=search_query)
        )
    
    if category:
        documents = documents.filter(category__icontains=category)
    
    # Get unique categories for filter dropdown
    categories = _get_scorecard_document_categories()
    
    paginator = Paginator(documents, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        'documents': list(page_obj.object_list),
        'page_obj': page_obj,
        'page_start': (page_obj.number - 1) * paginator.per_page,
        'page_end': min(page_obj.number * paginator.per_page, paginator.count),
        'query_string': _query_string_without_page(request),
        'categories': categories,
        'page_size': page_size,
        'filters': {
            'category': category,
            'search': search_query,
        },
        'total_count': paginator.count,
        'can_manage_documents': _can_manage_documents(request),
    }
    
    return render(
        request,
        'credit_scoreshifts/media/scorecard_document_list.html',
        context,
    )


@login_required
def scorecard_document_upload_view(request: HttpRequest) -> HttpResponse:
    """
    Upload a new scorecard document.
    """
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        category = request.POST.get('category', '').strip()
        uploaded_file = request.FILES.get('file')
        
        if not title:
            messages.error(request, 'Title is required.')
        elif not uploaded_file:
            messages.error(request, 'Please select a file to upload.')
        else:
            document = ScorecardDocument.objects.create(
                title=title,
                description=description,
                category=category,
                file=uploaded_file,
                file_name=uploaded_file.name,
                uploaded_by=request.user,
            )
            messages.success(request, f'Document "{title}" uploaded successfully!')
            return redirect('scorecard:scorecard_document_list')
    
    return render(
        request,
        'credit_scoreshifts/media/scorecard_document_upload.html',
    )


@login_required
def scorecard_document_detail_view(request: HttpRequest, document_id: int) -> HttpResponse:
    """
    View details of a specific scorecard document.
    """
    document = get_object_or_404(
        ScorecardDocument.objects.select_related('uploaded_by'),
        id=document_id,
        is_active=True
    )
    
    context = {
        'document': document,
    }
    
    return render(
        request,
        'credit_scoreshifts/media/scorecard_document_detail.html',
        context,
    )


@login_required
def scorecard_document_download_view(request: HttpRequest, document_id: int) -> HttpResponse:
    """
    Download a scorecard document.
    """
    document = get_object_or_404(
        ScorecardDocument,
        id=document_id,
        is_active=True
    )
    
    response = FileResponse(document.file.open(), as_attachment=True, filename=document.file_name)
    return response


@login_required
def document_delete_view(request: HttpRequest, document_id: int) -> HttpResponse:
    if request.method != 'POST':
        return redirect('scorecard:document_list')
    if not _can_manage_documents(request):
        messages.error(request, 'You do not have permission to delete evaluation documents.')
        return redirect('scorecard:document_list')

    branch_names = get_request_branch_names(request)
    queryset = AttributeResponseDocument.objects.select_related(
        'attribute_response__evaluation',
        'attribute_response__attribute',
        'attribute_response__option',
    )
    if not request.user.is_superuser and not branch_names:
        queryset = queryset.none()
    elif branch_names:
        queryset = queryset.filter(attribute_response__evaluation__branch_name__in=branch_names)

    document = get_object_or_404(queryset, id=document_id)
    file_name = document.file_name
    log_evaluation_document_audit(request.user, 'delete', document, 'basel')
    if document.file:
        document.file.delete(save=False)
    document.delete()
    messages.success(request, f'Document "{file_name}" deleted successfully.')
    return redirect('scorecard:document_list')


@login_required
def document_ifrs9_delete_view(request: HttpRequest, document_id: int) -> HttpResponse:
    if request.method != 'POST':
        return redirect('scorecard:document_list')
    if not _can_manage_documents(request):
        messages.error(request, 'You do not have permission to delete evaluation documents.')
        return redirect('scorecard:document_list')

    branch_names = get_request_branch_names(request)
    queryset = IFRS9AttributeResponseDocument.objects.select_related(
        'attribute_response__evaluation',
        'attribute_response__attribute',
        'attribute_response__option',
    )
    if not request.user.is_superuser and not branch_names:
        queryset = queryset.none()
    elif branch_names:
        queryset = queryset.filter(attribute_response__evaluation__branch_name__in=branch_names)

    document = get_object_or_404(queryset, id=document_id)
    file_name = document.file_name
    log_evaluation_document_audit(request.user, 'delete', document, 'ifrs9')
    if document.file:
        document.file.delete(save=False)
    document.delete()
    messages.success(request, f'Document "{file_name}" deleted successfully.')
    return redirect('scorecard:document_list')


@login_required
def scorecard_document_delete_view(request: HttpRequest, document_id: int) -> HttpResponse:
    if request.method != 'POST':
        return redirect('scorecard:scorecard_document_list')
    if not _can_manage_documents(request):
        messages.error(request, 'You do not have permission to delete scorecard documents.')
        return redirect('scorecard:scorecard_document_list')

    document = get_object_or_404(ScorecardDocument, id=document_id, is_active=True)
    title = document.title
    log_scorecard_document_audit(request.user, 'delete', document)
    if document.file:
        document.file.delete(save=False)
    document.delete()
    messages.success(request, f'Document "{title}" deleted successfully.')
    return redirect('scorecard:scorecard_document_list')
