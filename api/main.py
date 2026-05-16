from fastapi import FastAPI, HTTPException, Depends, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from google.oauth2 import id_token
from google.auth.transport import requests
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError
import os
import base64
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()

# Configuration (loaded from environment; .env values override defaults)
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb+srv://scholarsite:1LdbHC9zmSZxWKrR@cluster0.djrvguc.mongodb.net/?appName=Cluster0")
DATABASE_NAME = os.getenv("DATABASE_NAME", "scholarsite")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production-xyz123")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", str(24 * 7)))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com")

app = FastAPI()

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
        "http://localhost:3000",
        "https://scholar-site.vercel.app",
        "https://scholar-site-mu.vercel.app",
        "https://scholar-site-j02vo44mu-taufeeqs-projects-dbb5c752.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MongoDB connection
client = AsyncIOMotorClient(MONGODB_URL)
db = client[DATABASE_NAME]
users_collection = db.users
bookmarks_collection = db.bookmarks
opportunities_collection = db.opportunities
tips_collection = db.tips
feedback_collection = db.feedback
subscriptions_collection = db.subscriptions

# Security
security = HTTPBearer()

# Pydantic Models
class GoogleAuthRequest(BaseModel):
    credential: str

class UserUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    bio: Optional[str] = None

class BookmarkRequest(BaseModel):
    opportunityId: int

class FeedbackRequest(BaseModel):
    title: str
    body: str

class SubscriptionRequest(BaseModel):
    email: EmailStr
    states: List[str]
    categories: List[str]

# Helper Functions
def create_access_token(user_id: str) -> str:
    """Create JWT token"""
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "user_id": user_id,
        "exp": expire
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Verify JWT token and return user"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user = await users_collection.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        return user
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_optional_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> Optional[dict]:
    """Get user if authenticated, None otherwise (for optional auth)"""
    if not credentials:
        return None
    try:
        return await verify_token(credentials)
    except:
        return None

def serialize_user(user: dict) -> dict:
    """Convert MongoDB user to API response format"""
    return {
        "_id": str(user["_id"]),
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture"),
        "createdAt": user.get("createdAt"),
        "updatedAt": user.get("updatedAt"),
        "phone": user.get("phone"),
        "location": user.get("location"),
        "bio": user.get("bio")
    }


def parse_deadline(deadline_value):
    if isinstance(deadline_value, datetime):
        return deadline_value

    if isinstance(deadline_value, str):
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"]:
            try:
                return datetime.strptime(deadline_value, fmt)
            except ValueError:
                continue

    return None


def is_active_opportunity(opportunity: dict) -> bool:
    deadline = parse_deadline(opportunity.get("deadline"))
    if deadline is None:
        return True
    return deadline >= datetime.utcnow()

# -----------------------------
# Tips collection is stored in MongoDB
# -----------------------------

# -----------------------------
# Startup event - Create indexes
# -----------------------------
@app.on_event("startup")
async def startup_db_client():
    """Create database indexes on startup"""
    # Create indexes for better performance
    await users_collection.create_index("email", unique=True)
    await users_collection.create_index("googleId")
    await bookmarks_collection.create_index([("userId", 1), ("opportunityId", 1)], unique=True)
    await opportunities_collection.create_index("id", unique=True)
    await tips_collection.create_index("createdAt")
    await feedback_collection.create_index("timestamp")
    await subscriptions_collection.create_index("email")

    print("MongoDB connected and indexes created")

@app.on_event("shutdown")
async def shutdown_db_client():
    """Close MongoDB connection on shutdown"""
    client.close()

# =============================================================================
# AUTHENTICATION ENDPOINTS
# =============================================================================

@app.get("/")
async def root():
    return {"message": "ScholarSite API", "version": "2.0.0", "database": "MongoDB"}

@app.post("/api/auth/google")
async def google_auth(auth_request: GoogleAuthRequest):
    """Authenticate user with Google OAuth"""
    try:
        # Verify the Google token
        idinfo = id_token.verify_oauth2_token(
            auth_request.credential,
            requests.Request(),
            GOOGLE_CLIENT_ID
        )
        
        # Extract user information
        google_id = idinfo['sub']
        email = idinfo['email']
        name = idinfo['name']
        picture = idinfo.get('picture')
        
        # Check if user exists
        user = await users_collection.find_one({"email": email})
        
        if user:
            # Update existing user
            await users_collection.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {
                        "name": name,
                        "picture": picture,
                        "updatedAt": datetime.utcnow()
                    }
                }
            )
            user = await users_collection.find_one({"_id": user["_id"]})
        else:
            # Create new user
            new_user = {
                "googleId": google_id,
                "email": email,
                "name": name,
                "picture": picture,
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow()
            }
            result = await users_collection.insert_one(new_user)
            user = await users_collection.find_one({"_id": result.inserted_id})
        
        # Generate JWT token
        token = create_access_token(str(user["_id"]))
        
        return {
            "user": serialize_user(user),
            "token": token
        }
        
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Authentication failed: {str(e)}")

@app.post("/auth/google")
async def google_auth_alias(auth_request: GoogleAuthRequest):
    """Legacy endpoint alias for Google auth."""
    return await google_auth(auth_request)

@app.get("/api/auth/me")
async def get_current_user(user: dict = Depends(verify_token)):
    """Get current authenticated user"""
    return serialize_user(user)

@app.put("/api/auth/profile")
async def update_profile(
    updates: UserUpdate,
    user: dict = Depends(verify_token)
):
    """Update user profile"""
    update_data = {k: v for k, v in updates.dict().items() if v is not None}
    update_data["updatedAt"] = datetime.utcnow()
    
    await users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": update_data}
    )
    
    updated_user = await users_collection.find_one({"_id": user["_id"]})
    return serialize_user(updated_user)

@app.post("/api/auth/profile/picture")
async def upload_profile_picture(
    picture: UploadFile = File(...),
    user: dict = Depends(verify_token)
):
    """Upload profile picture"""
    # Validate file type
    if not picture.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    # Validate file size (max 5MB)
    contents = await picture.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size must be less than 5MB")
    
    # Store as base64
    base64_image = f"data:{picture.content_type};base64,{base64.b64encode(contents).decode()}"
    
    # Update user profile
    await users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "picture": base64_image,
                "updatedAt": datetime.utcnow()
            }
        }
    )
    
    return {"pictureUrl": base64_image}

@app.delete("/api/auth/account")
async def delete_account(user: dict = Depends(verify_token)):
    """Delete user account"""
    # Delete user's bookmarks
    await bookmarks_collection.delete_many({"userId": user["_id"]})
    
    # Delete user's subscriptions
    await subscriptions_collection.delete_many({"userId": user["_id"]})
    
    # Delete user
    await users_collection.delete_one({"_id": user["_id"]})
    
    return {"message": "Account deleted successfully"}

# =============================================================================
# OPPORTUNITIES ENDPOINTS
# =============================================================================

@app.get("/opportunities")
async def get_opportunities():
    """Get all active opportunities from MongoDB"""
    opportunities = await opportunities_collection.find().to_list(length=1000)
    active_opps = [opp for opp in opportunities if is_active_opportunity(opp)]
    return [
        {
            "id": opp["id"],
            "title": opp["title"],
            "description": opp["description"],
            "category": opp["category"],
            "state": opp["state"],
            "cost": opp.get("cost"),
            "deadline": opp.get("deadline"),
            "location": opp.get("location"),
            "sourceLink": opp.get("sourceLink")
        }
        for opp in active_opps
    ]

# =============================================================================
# BOOKMARKS ENDPOINTS
# =============================================================================

@app.get("/api/bookmarks")
async def get_bookmarks(user: dict = Depends(verify_token)) -> List[int]:
    """Get user's bookmarks"""
    bookmarks = await bookmarks_collection.find({"userId": user["_id"]}).to_list(length=1000)
    return [bookmark["opportunityId"] for bookmark in bookmarks]

@app.post("/api/bookmarks")
async def add_bookmark(
    request: BookmarkRequest,
    user: dict = Depends(verify_token)
):
    """Add bookmark"""
    # Check if already bookmarked
    existing = await bookmarks_collection.find_one({
        "userId": user["_id"],
        "opportunityId": request.opportunityId
    })
    
    if existing:
        return {"message": "Already bookmarked", "status": "exists"}
    
    bookmark = {
        "userId": user["_id"],
        "opportunityId": request.opportunityId,
        "createdAt": datetime.utcnow()
    }
    
    await bookmarks_collection.insert_one(bookmark)
    return {"message": "Bookmark added", "status": "added", "id": request.opportunityId}

@app.delete("/api/bookmarks/{opportunity_id}")
async def remove_bookmark(
    opportunity_id: int,
    user: dict = Depends(verify_token)
):
    """Remove bookmark"""
    result = await bookmarks_collection.delete_one({
        "userId": user["_id"],
        "opportunityId": opportunity_id
    })
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    
    return {"message": "Bookmark removed", "status": "removed", "id": opportunity_id}

# Legacy endpoints for backward compatibility (non-authenticated)
@app.get("/bookmarks")
async def get_bookmarks_legacy():
    """Legacy endpoint - returns empty for non-authenticated users"""
    return []

@app.post("/bookmarks/{id}")
async def add_bookmark_legacy(id: int):
    """Legacy endpoint - requires authentication now"""
    raise HTTPException(status_code=401, detail="Please sign in to bookmark opportunities")

@app.delete("/bookmarks/{id}")
async def remove_bookmark_legacy(id: int):
    """Legacy endpoint - requires authentication now"""
    raise HTTPException(status_code=401, detail="Please sign in to manage bookmarks")

# =============================================================================
# TIPS ENDPOINTS
# =============================================================================

@app.get("/tips")
async def get_tips():
    """Get all tips from MongoDB"""
    tip_docs = await tips_collection.find().to_list(length=100)
    return [
        {
            "id": str(doc.get("_id")),
            "title": doc.get("title", ""),
            "body": doc.get("body", ""),
            "createdAt": doc.get("createdAt")
        }
        for doc in tip_docs
    ]

# =============================================================================
# RECOMMENDATIONS ENDPOINTS
# =============================================================================

@app.get("/recommendations")
async def get_recommendations():
    """Get recommended opportunities"""
    opportunities = await opportunities_collection.find().sort("id", 1).to_list(length=100)
    active_opportunities = [opp for opp in opportunities if is_active_opportunity(opp)]
    return {
        "recommendations": active_opportunities[:5]
    }

# =============================================================================
# FEEDBACK ENDPOINTS
# =============================================================================

@app.get("/api/feedback")
async def get_feedback():
    """Get all feedback"""
    feedback_list = await feedback_collection.find().sort("timestamp", -1).to_list(length=100)
    return [
        {
            "id": str(f["_id"]),
            "title": f.get("title", ""),
            "body": f.get("body", ""),
            "timestamp": f.get("timestamp", "")
        }
        for f in feedback_list
    ]

@app.post("/api/feedback")
async def send_feedback(data: FeedbackRequest):
    """Submit feedback"""
    feedback = {
        "title": data.title,
        "body": data.body,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    result = await feedback_collection.insert_one(feedback)
    
    return {
        "status": "ok",
        "id": str(result.inserted_id),
        "message": "Feedback received"
    }

# Legacy endpoint
@app.get("/feedback")
async def get_feedback_legacy():
    """Legacy feedback endpoint"""
    return await get_feedback()

@app.post("/feedback")
async def send_feedback_legacy(data: dict):
    """Legacy feedback endpoint"""
    feedback_data = FeedbackRequest(
        title=data.get("title", ""),
        body=data.get("body", "")
    )
    return await send_feedback(feedback_data)

# =============================================================================
# SUBSCRIPTIONS ENDPOINTS
# =============================================================================

@app.get("/api/subscriptions")
async def get_subscriptions(user: dict = Depends(verify_token)):
    """Get user's subscriptions"""
    subscriptions = await subscriptions_collection.find(
        {"userId": user["_id"]}
    ).to_list(length=100)
    
    return [
        {
            "id": str(s["_id"]),
            "email": s.get("email", ""),
            "states": s.get("states", []),
            "categories": s.get("categories", []),
            "created_at": s.get("created_at", "")
        }
        for s in subscriptions
    ]

@app.post("/api/subscriptions")
async def subscribe(data: SubscriptionRequest, user: dict = Depends(verify_token)):
    """Create subscription"""
    subscription = {
        "userId": user["_id"],
        "email": data.email,
        "states": data.states,
        "categories": data.categories,
        "created_at": datetime.utcnow().isoformat()
    }
    
    result = await subscriptions_collection.insert_one(subscription)
    
    return {
        "status": "subscribed",
        "id": str(result.inserted_id)
    }

@app.delete("/api/subscriptions/{subscription_id}")
async def unsubscribe(subscription_id: str, user: dict = Depends(verify_token)):
    """Delete subscription"""
    result = await subscriptions_collection.delete_one({
        "_id": ObjectId(subscription_id),
        "userId": user["_id"]
    })
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    return {
        "status": "unsubscribed",
        "id": subscription_id
    }

# Legacy endpoints
@app.get("/subscriptions")
async def get_subscriptions_legacy():
    """Legacy subscriptions endpoint"""
    return []

@app.post("/subscriptions")
async def subscribe_legacy(data: dict):
    """Legacy subscription endpoint - requires auth"""
    raise HTTPException(status_code=401, detail="Please sign in to subscribe")

@app.delete("/subscriptions/{id}")
async def unsubscribe_legacy(id: int):
    """Legacy unsubscribe endpoint - requires auth"""
    raise HTTPException(status_code=401, detail="Please sign in to manage subscriptions")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
