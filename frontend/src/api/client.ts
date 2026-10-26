import { config } from "../app/config";
import type { ApiErrorShape } from "../types/domain";

const ERROR_CODE_MESSAGES: Record<string, string> = {
  AUTH_REQUIRED: "Vui lòng đăng nhập để tiếp tục.",
  AUTH_NOT_CONFIGURED: "Hệ thống xác thực chưa được cấu hình. Vui lòng liên hệ quản trị viên.",
  UNAUTHORIZED: "Phiên đăng nhập đã hết hạn hoặc không hợp lệ. Vui lòng đăng nhập lại.",
  FORBIDDEN: "Bạn không có quyền thực hiện thao tác này.",
  PERMISSION_DENIED: "Bạn không có quyền thực hiện thao tác này.",
  INVALID_CREDENTIALS: "Email hoặc mật khẩu không chính xác.",
  EMAIL_ALREADY_REGISTERED: "Email này đã được đăng ký tài khoản.",
  EMAIL_NOT_VERIFIED: "Email này chưa được xác minh. Vui lòng nhập mã xác thực đã gửi tới hộp thư của bạn.",
  OTP_INVALID: "Mã xác thực không chính xác hoặc đã hết hạn.",
  OTP_MAX_ATTEMPTS: "Bạn đã nhập sai mã xác thực quá nhiều lần. Vui lòng yêu cầu gửi lại mã mới.",
  EMAIL_DELIVERY_FAILED: "Không thể gửi email lúc này. Bạn hãy sao chép liên kết chia sẻ trực tiếp nhé.",
  EMAIL_DELIVERY_UNAVAILABLE: "Hệ thống gửi email chưa được cấu hình. Bạn hãy sao chép liên kết chia sẻ trực tiếp nhé.",

  MEETING_NOT_FOUND: "Không tìm thấy cuộc họp này.",
  USER_NOT_FOUND: "Email này chưa có tài khoản trên hệ thống. Người nhận cần đăng ký tài khoản trước.",
  MEMBER_ALREADY_EXISTS: "Người dùng này đã là thành viên của cuộc họp rồi.",
  MEMBER_NOT_FOUND: "Không tìm thấy thành viên này trong cuộc họp.",
  SHARE_REVOKED: "Liên kết chia sẻ này đã bị thu hồi.",
  SHARE_INVALID: "Liên kết chia sẻ không hợp lệ, đã hết hạn hoặc bị thu hồi.",
  SHARE_NOT_FOUND: "Không tìm thấy liên kết chia sẻ này.",

  MEDIA_UNAVAILABLE: "Tệp âm thanh này được tạo từ phiên bản trước đó nên không còn trên máy chủ. Toàn bộ nội dung văn bản bên dưới vẫn được lưu an toàn.",
  MEDIA_NOT_FOUND: "Không tìm thấy tệp âm thanh này.",
  MEDIA_INVALID: "Tệp âm thanh không hợp lệ hoặc vượt quá thời lượng cho phép.",
  MEDIA_STORAGE_ERROR: "Không thể tải hoặc đọc tệp âm thanh từ kho lưu trữ. Vui lòng thử lại sau.",
  MEDIA_RANGE_INVALID: "Khoảng âm thanh yêu cầu phát không hợp lệ.",

  TRANSCRIPT_NOT_FOUND: "Chưa có nội dung bóc băng cho cuộc họp này.",
  TRANSCRIPT_INVALID: "Nội dung bóc băng không hợp lệ hoặc đang trống.",
  TRANSCRIPT_VERSION_NOT_FOUND: "Không tìm thấy phiên bản bóc băng này.",
  TRANSCRIPT_PROJECTION_UNAVAILABLE: "Hệ thống đang đồng bộ dữ liệu, vui lòng thử lại sau giây lát.",
  SEGMENT_NOT_FOUND: "Không tìm thấy đoạn nội dung này.",
  SEGMENTS_NOT_MERGEABLE: "Chỉ có thể gộp các đoạn nằm liền kề nhau của cùng một người nói.",
  INVALID_SEGMENT_TIMESTAMPS: "Mốc thời gian của đoạn không hợp lệ.",
  INVALID_SEGMENT_SPLIT: "Cả hai đoạn sau khi tách đều phải có nội dung văn bản.",
  INVALID_SEGMENT_ORDER: "Các đoạn nội dung phải theo đúng thứ tự thời gian và không được đè lên nhau.",
  INVALID_SEGMENT_TEXT: "Nội dung đoạn văn bản không được để trống.",
  INVALID_TRANSCRIPT_CURSOR: "Vị trí con trỏ văn bản không hợp lệ.",

  SPEAKER_NOT_FOUND: "Không tìm thấy thông tin người nói.",
  SPEAKER_SAMPLE_UNAVAILABLE: "Chưa có đoạn âm thanh mẫu cho người nói này.",
  VOICE_PROFILE_NOT_FOUND: "Không tìm thấy mẫu giọng nói.",
  VOICE_AUDIO_NOT_FOUND: "Chưa có tệp âm thanh cho mẫu giọng này.",
  SAMPLE_NOT_FOUND: "Không tìm thấy lần thu âm này.",
  INVALID_NAME: "Tên người nói không được để trống.",

  EXPORT_NOT_READY: "Tài liệu đang được xử lý, vui lòng chờ trong giây lát.",
  EXPORT_STORAGE_ERROR: "Không thể tải tệp xuất ra từ kho lưu trữ. Vui lòng thử lại sau.",
  EXPORT_NOT_FOUND: "Không tìm thấy tệp xuất này.",

  TRANSLATION_NOT_FOUND: "Chưa có bản dịch cho cuộc họp này.",
  SUMMARY_NOT_FOUND: "Chưa có bản tóm tắt cho cuộc họp này.",
  SUMMARY_VERSION_NOT_FOUND: "Không tìm thấy phiên bản tóm tắt này.",
  PROVIDER_CAPABILITY_UNAVAILABLE: "Tính năng AI này tạm thời chưa khả dụng.",

  STT_MEDIA_UNAVAILABLE: "Tệp âm thanh không khả dụng để bóc băng.",
  STT_TRANSCRIPTION_FAILED: "Quá trình nhận dạng giọng nói thất bại. Vui lòng thử lại với tệp âm thanh khác.",
  STT_TIMEOUT: "Quá trình nhận dạng giọng nói mất nhiều thời gian hơn dự kiến. Vui lòng thử lại sau.",
  STT_RESULT_INVALID: "Dữ liệu nhận dạng giọng nói không hợp lệ.",
  STT_LANGUAGE_UNSUPPORTED: "Ngôn ngữ được chọn chưa được hỗ trợ bóc băng trực tiếp.",
  GEMINI_NETWORK_ERROR: "Không thể kết nối máy chủ AI. Vui lòng thử lại sau.",
  GROQ_NETWORK_ERROR: "Không thể kết nối máy chủ AI. Vui lòng thử lại sau.",

  VALIDATION_ERROR: "Thông tin nhập vào chưa hợp lệ. Vui lòng kiểm tra lại các trường dữ liệu.",
  INTERNAL_SERVER_ERROR: "Hệ thống gặp sự cố máy chủ tạm thời. Vui lòng thử lại sau giây lát.",
  NOT_FOUND: "Không tìm thấy dữ liệu yêu cầu.",
  HTTP_ERROR: "Đã có lỗi xảy ra khi gửi yêu cầu tới máy chủ.",
  NETWORK_ERROR: "Không thể kết nối máy chủ. Vui lòng kiểm tra lại kết nối mạng.",
  JOB_STATUS_UNAVAILABLE: "Không thể kiểm tra tiến độ xử lý lúc này.",
};

const ENGLISH_PATTERNS: Array<[RegExp, string]> = [
  [/Media is no longer available/i, "Tệp âm thanh này được tạo từ phiên bản trước đó nên không còn trên máy chủ. Toàn bộ nội dung văn bản bên dưới vẫn được lưu an toàn."],
  [/Media was not found/i, "Không tìm thấy tệp âm thanh này."],
  [/Media could not be read/i, "Không thể đọc tệp âm thanh lúc này."],
  [/Media could not be stored/i, "Không thể lưu tệp âm thanh vào kho lưu trữ."],
  [/The requested media range is invalid/i, "Khoảng âm thanh yêu cầu phát không hợp lệ."],
  [/No user exists for this invitation/i, "Email này chưa có tài khoản trên hệ thống. Người nhận cần đăng ký tài khoản trước."],
  [/This user is already a collaborator/i, "Người dùng này đã là thành viên của cuộc họp rồi."],
  [/Collaborator was not found/i, "Không tìm thấy thành viên này trong cuộc họp."],
  [/This share link is invalid, expired, or revoked/i, "Liên kết chia sẻ không hợp lệ, đã hết hạn hoặc bị thu hồi."],
  [/A revoked share link cannot be changed/i, "Liên kết chia sẻ này đã bị thu hồi."],
  [/Share link was not found/i, "Không tìm thấy liên kết chia sẻ này."],
  [/Transcript segment was not found/i, "Không tìm thấy đoạn bóc băng này."],
  [/No transcript exists for this meeting/i, "Chưa có nội dung bóc băng cho cuộc họp này."],
  [/Speaker was not found/i, "Không tìm thấy thông tin người nói."],
  [/No playable media is available/i, "Chưa có tệp âm thanh cho người nói này."],
  [/No transcript sample is available/i, "Chưa có đoạn mẫu bóc băng cho người nói này."],
  [/Segments must be contiguous and have the same speaker/i, "Chỉ có thể gộp hai đoạn liền kề của cùng một người nói."],
  [/Segments must be adjacent in the current transcript/i, "Các đoạn được chọn phải nằm liền nhau."],
  [/split_at_ms must fall inside the segment/i, "Điểm chia đoạn phải nằm trong khoảng thời gian của đoạn."],
  [/Both split segments require text/i, "Cả hai đoạn sau khi tách đều phải có nội dung văn bản."],
  [/Each segment must have start_ms earlier than end_ms/i, "Thời điểm bắt đầu phải nhỏ hơn thời điểm kết thúc."],
  [/Transcript segment text must not be empty/i, "Nội dung đoạn văn bản không được để trống."],
  [/Transcript segments must be chronologically ordered/i, "Các đoạn nội dung phải theo đúng thứ tự thời gian."],
  [/An account already exists for this email/i, "Email này đã được đăng ký tài khoản."],
  [/Invalid email or password/i, "Email hoặc mật khẩu không chính xác."],
  [/Verify your email before signing in/i, "Vui lòng xác minh địa chỉ email của bạn trước khi đăng nhập."],
  [/The verification code is invalid or expired/i, "Mã xác thực không chính xác hoặc đã hết hạn."],
  [/Authentication is required/i, "Vui lòng đăng nhập để tiếp tục."],
  [/You do not have access to this meeting/i, "Bạn không có quyền truy cập cuộc họp này."],
  [/You do not have permission for this action/i, "Bạn không có quyền thực hiện thao tác này."],
  [/The export is not ready to download/i, "Tài liệu đang được tạo, vui lòng chờ trong giây lát."],
  [/The export file is unavailable/i, "Không thể tải tệp xuất lúc này."],
  [/A transcript is required for export/i, "Cần có nội dung bóc băng trước khi xuất tệp."],
  [/A transcript is required for this operation/i, "Cần có nội dung bóc băng để thực hiện thao tác này."],
  [/Translation is not configured/i, "Tính năng dịch tự động chưa được cấu hình."],
  [/AI summary generation is not configured/i, "Tính năng tóm tắt AI chưa được cấu hình."],
  [/This translation language pair is not supported/i, "Cặp ngôn ngữ dịch này hiện chưa được hỗ trợ."],
  [/Only the comment author may update it/i, "Chỉ người viết bình luận mới có thể chỉnh sửa."],
  [/AssemblyAI transcription failed/i, "Quá trình nhận dạng giọng nói thất bại."],
  [/AssemblyAI transcription timed out/i, "Quá trình nhận dạng giọng nói quá thời gian chờ."],
  [/Gemini could not be reached/i, "Không thể kết nối máy chủ AI."],
  [/Groq could not be reached/i, "Không thể kết nối máy chủ AI."],
  [/Failed to fetch/i, "Không thể kết nối tới máy chủ. Vui lòng kiểm tra lại kết nối mạng."],
  [/NetworkError/i, "Lỗi kết nối mạng. Vui lòng kiểm tra kết nối mạng và thử lại."],
  [/Not Found/i, "Không tìm thấy dữ liệu yêu cầu."],
  [/Internal Server Error/i, "Hệ thống gặp sự cố máy chủ tạm thời. Vui lòng thử lại sau giây lát."],
  [/Request validation failed/i, "Dữ liệu nhập vào chưa hợp lệ. Vui lòng kiểm tra lại."],
];

export function translateErrorMessage(code?: string, rawMessage?: unknown): string {
  if (code && ERROR_CODE_MESSAGES[code]) {
    return ERROR_CODE_MESSAGES[code];
  }
  const text = typeof rawMessage === "string" ? rawMessage.trim() : "";
  if (text) {
    for (const [pattern, translation] of ENGLISH_PATTERNS) {
      if (pattern.test(text)) {
        return translation;
      }
    }
    return text;
  }
  return "Đã có lỗi xảy ra. Vui lòng thử lại.";
}

export class ApiError extends Error {
  constructor(public status: number, public code?: string, message = "Đã có lỗi xảy ra.") {
    super(translateErrorMessage(code, message));
  }
}

let getAccessToken: () => string | null = () => null;
let onUnauthorized: () => Promise<boolean> = async () => false;
let refreshInFlight: Promise<boolean> | undefined;
export function configureApiAuth(token: () => string | null, refresh: () => Promise<boolean>) { getAccessToken = token; onUnauthorized = refresh; }

function refreshAccessToken(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = onUnauthorized().finally(() => { refreshInFlight = undefined; });
  }
  return refreshInFlight;
}

async function request<T>(path: string, init: RequestInit = {}, retry = true, responseType: "json" | "blob" = "json"): Promise<T> {
  // Auth endpoints must not try to refresh themselves. Otherwise an expired
  // refresh token can recursively call /auth/refresh after its own 401.
  const isPublicAuthRequest = path.startsWith("/auth/") && !path.startsWith("/auth/me");
  const token = isPublicAuthRequest ? null : getAccessToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const fetchOptions = { ...init, headers, credentials: "include" as const };
  let response: Response;
  try {
    response = await fetch(`${config.apiBaseUrl}${path}`, fetchOptions);
  } catch (cause) {
    if (init.signal?.aborted) throw cause;
    const method = init.method?.toUpperCase() ?? "GET";
    if (method !== "GET") {
      throw new ApiError(0, "NETWORK_ERROR", "Không thể kết nối máy chủ. Vui lòng thử lại.");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    try {
      response = await fetch(`${config.apiBaseUrl}${path}`, fetchOptions);
    } catch (retryCause) {
      if (init.signal?.aborted) throw retryCause;
      throw new ApiError(0, "NETWORK_ERROR", "Không thể kết nối máy chủ. Vui lòng thử lại.");
    }
  }
  if (response.status === 401 && !isPublicAuthRequest && retry && await refreshAccessToken()) return request<T>(path, init, false, responseType);
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorShape & { detail?: unknown };
    let message: string | undefined;
    if (typeof body.message === "string" && body.message.trim()) {
      message = body.message;
    } else if (typeof body.detail === "string" && body.detail.trim()) {
      message = body.detail;
    } else if (Array.isArray(body.detail) && body.detail.length > 0) {
      message = "Dữ liệu nhập vào chưa đúng định dạng. Vui lòng kiểm tra lại.";
    }
    throw new ApiError(response.status, body.code, message ?? "Không thể hoàn tất yêu cầu.");
  }
  if (response.status === 204) return undefined as T;
  return (responseType === "blob" ? response.blob() : response.json()) as Promise<T>;
}

export const apiClient = {
  get: <T>(path: string, init: RequestInit = {}) => request<T>(path, init),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  getBlob: (path: string) => request<Blob>(path, {}, true, "blob"),
  postBlob: (path: string, body?: unknown) => request<Blob>(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body) }, true, "blob"),
};
