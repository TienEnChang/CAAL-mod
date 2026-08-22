interface LayoutProps {
  children: React.ReactNode;
}

export default function Layout({ children }: LayoutProps) {
  return (
    <>
      {children}

      {/* Branding - bottom right */}
      <footer className="fixed right-0 bottom-0 z-40 hidden p-6 md:block">
        <span className="text-muted-foreground font-mono text-xs font-medium tracking-wider uppercase">
          Built by{' '}
          <a
            target="_blank"
            rel="noopener noreferrer"
            href="https://github.com/coreworxlab"
            className="hover:text-foreground underline underline-offset-4 transition-colors"
          >
            CoreWorxLab
          </a>
        </span>
      </footer>
    </>
  );
}
