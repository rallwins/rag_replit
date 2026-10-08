import { type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import { Shell } from '@/components/workspace';
import HomePage from '@/pages/home';
import CollectionPage from '@/pages/collection';
import SettingsPage from '@/pages/settings';
import NotFound from '@/pages/not-found';
import { Route, Switch, useLocation, Router as WouterRouter } from 'wouter';
import { useAuth } from '@workspace/replit-auth-web';

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 20_000, retry: 1, refetchOnWindowFocus: true } },
});

function Router({ user, onLogout }: { user: NonNullable<ReturnType<typeof useAuth>['user']>; onLogout: () => void }) {
  return <Shell user={user} onLogout={onLogout}><RoutedErrorBoundary><Switch>
    <Route path="/" component={HomePage}/>
    <Route path="/collections/:collectionId" component={CollectionPage}/>
    <Route path="/settings" component={SettingsPage}/>
    <Route component={NotFound}/>
  </Switch></RoutedErrorBoundary></Shell>;
}

function RoutedErrorBoundary({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  return <ErrorBoundary resetKey={location}>{children}</ErrorBoundary>;
}

function App() {
  return <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
        <AuthGate/>
      </WouterRouter>
      <Toaster/>
    </TooltipProvider>
  </QueryClientProvider>;
}

function AuthGate() {
  const auth = useAuth();
  if (auth.isLoading) {
    return <main className="auth-screen"><div className="auth-card"><div className="brand-mark" aria-hidden="true">F</div><div className="skeleton" style={{width:180,height:18,marginTop:20}}/><div className="skeleton" style={{width:240,height:12,marginTop:12}}/></div></main>;
  }
  if (!auth.isAuthenticated || !auth.user) {
    return <main className="auth-screen"><section className="auth-card"><span className="brand-mark" aria-hidden="true">F</span><p className="auth-kicker">PRIVATE KNOWLEDGE WORKSPACE</p><h1>Bring your documents into focus.</h1><p className="auth-copy">Sign in to organize your source material, ask grounded questions, and trace every answer back to its documents.</p><button className="button button-primary auth-button" data-testid="auth-sign-in" onClick={auth.login}>Continue to sign in</button><p className="auth-note">Your workspace is private to your account.</p></section></main>;
  }
  return <Router user={auth.user} onLogout={auth.logout}/>;
}

export default App;
