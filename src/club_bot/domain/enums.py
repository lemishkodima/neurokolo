from enum import StrEnum


class SubscriptionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


class PaymentStatus(StrEnum):
    APPROVED = "approved"
    DECLINED = "declined"
    PENDING = "pending"
    REFUNDED = "refunded"


class RecurringStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    MISSING = "missing"
    CREATED = "created"
    CONFIRMED = "confirmed"
    SUSPENDED = "suspended"
    REMOVED = "removed"
    COMPLETED = "completed"
    CHECK_FAILED = "check_failed"
    NOT_APPLICABLE = "not_applicable"


class CheckoutStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    CLAIMED = "claimed"
    EXPIRED = "expired"


class ResourceType(StrEnum):
    CHANNEL = "channel"
    SUPERGROUP = "supergroup"


class MembershipStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    REVOKED = "revoked"


class ReferralStatus(StrEnum):
    REGISTERED = "registered"
    QUALIFIED = "qualified"
    REWARDED = "rewarded"
    REJECTED = "rejected"


class RewardType(StrEnum):
    BONUS_DAYS = "bonus_days"
    DISCOUNT_PERCENT = "discount_percent"
    FIXED_CREDIT = "fixed_credit"


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENDING = "sending"
    COMPLETED = "completed"
    FAILED = "failed"


class BroadcastTarget(StrEnum):
    ALL_USERS = "all_users"
    ACTIVE_SUBSCRIBERS = "active_subscribers"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
