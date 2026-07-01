from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.ledger import UserRegisterRequest, UserResponse, TokenResponse
from app.services.auth_service import AuthService
from app.api.deps import get_current_user
from app.models.ledger import User

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(req: UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user."""
    svc = AuthService(db)
    user = await svc.register(req)
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """Obtain a JWT bearer token."""
    svc = AuthService(db)
    return await svc.login(form_data.username, form_data.password)


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    """Get currently authenticated user."""
    return UserResponse.model_validate(current_user)
